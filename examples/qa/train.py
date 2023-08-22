import random
import sys
import os
from typing import Dict

sys.path.append("/home/jovyan/nazar/filtered-transformer/")

from memup.base import DataCollectorAppend, DataCollectorReplace, MemoryRollout
from memup.loss import TS, LossModule, PredictorLossStateOnly
from metrics.accuracy import AccuracyMetric
from torch import Tensor, nn
from examples.qa.modules import DataFilter, MemUpMemoryImpl, Predictor, RobertaRT
from memup.base import DataCollectorAppend, MemoryRollout, State
from memup.loss import TS, LossModule, PredictorLossStateOnly
from metrics.accuracy import AccuracyMetric
from examples.qa.data import get_tokenized_dataset
from transformers import AutoConfig, AutoTokenizer
import transformers
import tasks
from transformers.file_utils import PaddingStrategy
from transformers.tokenization_utils_base import TruncationStrategy
from transformers import RobertaTokenizer, RobertaModel
from torch.nn.utils.rnn import pad_sequence
import torch
from transformers.data.data_collator import DataCollator, DataCollatorWithPadding, default_data_collator
from torch.utils.data import DataLoader, Dataset
from torch.optim import AdamW
from torch.utils.data import DataLoader, Dataset
from torch.utils.tensorboard import SummaryWriter
import numpy as np


def set_seed(seed: int = 42) -> None:
    np.random.seed(seed)
    random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    # When running on the CuDNN backend, two further options must be set
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
    # Set a fixed value for the hash seed
    os.environ["PYTHONHASHSEED"] = str(seed)
    print(f"Random seed set as {seed}")


set_seed(5)


def adjust_tokenizer(tokenizer):
    if isinstance(tokenizer, (transformers.GPT2Tokenizer, transformers.GPT2TokenizerFast)) and \
            "gpt" in tokenizer.name_or_path:
        tokenizer.pad_token = tokenizer.eos_token


tokenizer = RobertaTokenizer.from_pretrained(
    "roberta-base",
    cache_dir="/home/jovyan/cashe",
    use_fast=True,
    revision="main",
)

adjust_tokenizer(tokenizer)


model = RobertaRT(RobertaModel.from_pretrained(
    'roberta-base',
    cache_dir="/home/jovyan/cashe",
    revision="main",
)).cuda()

predictor = Predictor(model.bert.config).cuda()

# weights = torch.load("/home/jovyan/models/qa_5.240.pt", map_location="cpu")
# model.load_state_dict(weights["mem"])
# predictor.load_state_dict(weights["pred"])


task = tasks.get_task(task_args=tasks.TaskArguments(task_name="custom", task_base_path="/home/jovyan/nazar/quality_mc/"))
dataset_dict = task.get_datasets()

tokenized_dataset_dict = get_tokenized_dataset(
    task=task,
    dataset_dict=dataset_dict,
    tokenizer=tokenizer,
    max_seq_length=4096,
    padding_strategy=PaddingStrategy(PaddingStrategy.MAX_LENGTH),
    truncation_strategy=TruncationStrategy(TruncationStrategy.ONLY_FIRST),
    model_mode="mc",
)

train_data = tokenized_dataset_dict.get("train")
test_data = tokenized_dataset_dict.get("validation")

train_data, val_data = torch.utils.data.random_split(train_data, 
                                                     [int(len(train_data) * 0.8), len(train_data) - int(len(train_data) * 0.8)])

print("TRAIN size", len(train_data))
print("TEST size", len(test_data))
print("VAL size", len(val_data))

def collate_fn(batch):

    batch_pt = {}
        
    for k in ['input_ids', 'attention_mask', "label", 'input_part_token_start_idx']:
        batch_pt[k] = torch.stack(
            [torch.tensor(el[k]) for el in batch]
        )

    return batch_pt


train_dataloader = DataLoader(train_data, shuffle=True, batch_size=32, num_workers=8, collate_fn=collate_fn)
test_dataloader = DataLoader(test_data, shuffle=False, batch_size=128, num_workers=8, collate_fn=collate_fn)
val_dataloader = DataLoader(val_data, shuffle=False, batch_size=128, num_workers=8, collate_fn=collate_fn)

data_filter = DataFilter(tokenizer, 250)

memup_iter = MemoryRollout[Dict[str, Tensor]](
    steps=2,
    memory=MemUpMemoryImpl(model),
    data_filter=data_filter,
    info_update=[]
)

opt = AdamW([
    {"params": model.bert.parameters(), "lr": 1e-6},
    {"params": model.encoder.parameters(), "lr": 1e-5},
    {"params": predictor.parameters(), "lr": 1e-5},
] , weight_decay=1e-4)


class DataCollectorTrain(DataCollectorAppend[Dict[str, Tensor], Tensor]):
    def apply(self, data: Dict[str, Tensor], out: Tensor, state: State) -> Tensor:
        return state
    

class DataCollectorEval(DataCollectorReplace[Dict[str, Tensor], Tensor]):
    def apply(self, data: Dict[str, Tensor], out: Tensor, state: State) -> Tensor:
        return state


writer = SummaryWriter("/home/jovyan/pomoika/qa/2.7")
global_step = 0
batch_count = 0

for it in range(100):

    for batch in train_dataloader:
        batch_count += 1

        labels = batch["label"].cuda()

        state = torch.zeros(labels.shape[0] * 4, 30, 768, device=torch.device("cuda"))
        done = False
        info = {}

        model.train()
        predictor.train()

        # with torch.no_grad():
        #     _, last_state, _, _ = memup_iter.forward(batch, state, {}, DataCollectorEval(), 100)

        print(it, batch_count, global_step)

        if global_step > 3000:
            data_filter.always_add_promt = False

        grad_acc_times = 5

        while not done:
            global_step += 1

            data_collector, state, info, done = memup_iter.forward(batch, state, info, DataCollectorTrain())
            states_seq = data_collector.result()
            # pred = predictor(torch.cat([states_seq[-1], last_state], 1))
            pred = predictor(states_seq[-1])
            loss = nn.CrossEntropyLoss()(pred, labels)
            acc = AccuracyMetric()(pred, labels)

            print(pred.argmax(-1).reshape(-1).cpu().numpy())
            
            print(loss.item(), "acc=", acc)
            writer.add_scalar("loss", loss.item(), global_step)
            writer.add_scalar("acc", acc, global_step)

            (loss / grad_acc_times).backward()

            if global_step % grad_acc_times == 0:
                opt.step()
                opt.zero_grad()

        if batch_count % 30 == 0:
            # torch.save({
            #     "mem": model.state_dict(),
            #     "pred": predictor.state_dict()
            # }, f"/home/jovyan/models/qa_2.0.pt")

            print("TEST length ", len(test_data))
            data_filter.always_add_promt = True

            with torch.no_grad():

                all_pred = []
                all_labels = []

                for batch in test_dataloader:
                    labels = batch["label"].cuda()

                    state = torch.zeros(labels.shape[0] * 4, 30, 768, device=torch.device("cuda"))
                    done = False
                    info = {}

                    model.eval()
                    predictor.eval()

                    data_collector, last_state, _, _ = memup_iter.forward(batch, state, info, DataCollectorEval(), steps=1000)
                    pred = predictor(last_state)
                    loss = nn.CrossEntropyLoss()(pred, labels)
                    acc = AccuracyMetric()(pred, labels)

                    print(loss.item(), "acc=", acc)
                    writer.add_scalar("test loss", loss.item(), global_step)

                    all_pred.append(pred.detach().cpu())
                    all_labels.append(labels.cpu())
                
                acc = AccuracyMetric()(torch.cat(all_pred), torch.cat(all_labels))
                print("final test acc", acc)
                writer.add_scalar("test acc", acc, global_step)
            
            print("EVAL length ", len(val_data))

            with torch.no_grad():

                all_pred = []
                all_labels = []

                for batch in val_dataloader:
                    labels = batch["label"].cuda()

                    state = torch.zeros(labels.shape[0] * 4, 30, 768, device=torch.device("cuda"))
                    done = False
                    info = {}

                    model.eval()
                    predictor.eval()

                    data_collector, last_state, _, _ = memup_iter.forward(batch, state, info, DataCollectorEval(), steps=1000)
                    pred = predictor(last_state)
                    loss = nn.CrossEntropyLoss()(pred, labels)
                    acc = AccuracyMetric()(pred, labels)

                    print(loss.item(), "acc=", acc)
                    writer.add_scalar("val loss", loss.item(), global_step)

                    all_pred.append(pred.detach().cpu())
                    all_labels.append(labels.cpu())
                
                acc = AccuracyMetric()(torch.cat(all_pred), torch.cat(all_labels))
                print("final val acc", acc)
                writer.add_scalar("val acc", acc, global_step)







        