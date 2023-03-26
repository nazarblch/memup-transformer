from copy import deepcopy
import time
from tkinter.messagebox import NO
from types import new_class
from typing import Dict, Optional, Tuple
from typing_extensions import override
from common_modules.rmt import RecurrentTransformerWithStateEmbedding
from torch import Tensor, nn
import torch
from transformers.modeling_outputs import TokenClassifierOutput
from gena_lm.modeling_bert import BertPreTrainedModel, BertModel, BertEncoder
from common_modules.pos_encoding import PositionalEncoding2
from common_modules.transformers import DecoderFromBert
from data_filters.sliding_window import SlidingWindowFilter, SlidingWindowWithPadding
from memup.base import SD, DataCollectorAppend, Done, Info, MemUpMemory, SeqDataFilter, State
from torch.nn.utils.rnn import pad_sequence

from memup.loss import TOS, TOSM


class DataFilter(SeqDataFilter[Dict[str, Tensor]]):

    def __init__(self, step: int):
        super().__init__()
        self.center_step = step
        self.context_step = step
        pos_encoder = PositionalEncoding2(768 * 2, 0, 896)
        self.positions = pos_encoder.forward(torch.zeros(1, 896, 768 * 2)).cuda()

    @torch.no_grad()
    def forward(self, data: Dict[str, Dict[str, Tensor]], state: State, info: Info, *args) -> Tuple[Dict[str, Tensor], Done]:

        if "stage" not in info:
            info["stage"] = "left"
            print("stage", info["stage"])

        stage = info["stage"]

        BS = self.center_step if stage == "center" else self.context_step
        T = 896 if stage == "center" else data[stage]['input_ids'].shape[1]
        assert "step" in info
        step = info["step"]
    
        if stage == "left" and step * BS + BS >= T:
            info["stage"] = "center"
            info["step"] = -1
            print("stage", info["stage"])
            info["batch_step"] = torch.zeros(data["center"]['input_ids'].shape[0], dtype=torch.int32)
        
        i1 = step * BS
        i2 = i1 + BS
    
        if stage == "center":
            new_data = self.filter_center(data["center"], info)
            
            if info["batch_step"].min() >= T:
                info["stage"] = "right"
                info["step"] = -1
                print("stage", info["stage"])

            return new_data, False
        else:
            done = (step * BS + BS >= T) and (info["stage"] == "right")
            return self.filter_context(data[stage], i1, i2), done
    

    def filter_center(self, data: Dict[str, Tensor], info) -> Dict[str, Tensor]:

        feature_keys = ['input_ids', 'token_type_ids', 'attention_mask', 'bins_mask']
        pad_token_ids = {'input_ids': 3, 'token_type_ids': 0, 'attention_mask': 0, 'bins_mask': 0, "labels": 0, "labels_mask": 0, "positions": 0}
        new_data = {} 

        cusum = data['bins_mask'].type(torch.int32).cumsum(1)

        for k in feature_keys + [ "labels", "labels_mask", "positions"]:
            new_data[k] = []
    
        for i in range(cusum.shape[0]):
            i1 = info["batch_step"][i].item()
            i2 =  min(896, i1 + 3)
            mask = (cusum[i] > i1 * 2) * (cusum[i] < i2 * 2) + (cusum[i] == i2 * 2) * data['bins_mask'][i] * (i1 < i2) + (cusum[i] == i1 * 2) * (data['bins_mask'][i] == False) * (i1 < i2)
            mask_bk = mask

            assert mask.type(torch.int32).sum() < self.center_step

            while i2 < 896 and mask.type(torch.int32).sum() < self.center_step:
                i2 += 1
                mask_bk = mask
                mask = (cusum[i] > i1 * 2) * (cusum[i] < i2 * 2) + (cusum[i] == i2 * 2) * data['bins_mask'][i] + (cusum[i] == i1 * 2) * (data['bins_mask'][i] == False) 
                
            if mask.type(torch.int32).sum() > self.center_step:
                mask = mask_bk
                i2 = i2 - 1

            info["batch_step"][i] = i2

            if data['bins_mask'][i][mask].type(torch.int32).sum().item() != 2 * (i2 - i1):
                print(i1, i2, 2 * (i2 - i1), data['bins_mask'][i][mask].type(torch.int32).sum().item(), mask.type(torch.int32).sum())

            assert data['bins_mask'][i][mask].type(torch.int32).sum().item() == 2 * (i2 - i1)

            for k in feature_keys:
                new_data[k].append(data[k][i][mask])

            labels = data["labels"][i, i1: i2]
            new_data["labels"].append(labels)
            new_data["labels_mask"].append(torch.ones(labels.shape[0]).type(torch.bool))
            new_data["positions"].append(self.positions[0, i1: i2])

        for k in feature_keys + [ "labels", "labels_mask", "positions"]:
            new_data[k] = pad_sequence(new_data[k], batch_first=True, padding_value=pad_token_ids[k]).cuda()

        # print(new_data["labels"].shape, new_data["input_ids"].shape)

        return new_data
    
    def filter_context(self, data: Dict[str, Tensor], i1: int, i2: int) -> Dict[str, Tensor]:
        
        feature_keys = ['input_ids', 'token_type_ids', 'attention_mask']
        new_data = {} 
    
        for k in feature_keys:
            new_data[k] = data[k][:, i1: i2].cuda()

        return new_data
    


class BertForEnformer(BertPreTrainedModel):

    _keys_to_ignore_on_load_unexpected = [r"pooler"]

    def __init__(self, config):
        super().__init__(config)
        self.num_labels = config.num_labels
        self.config = config

        self.bert = BertModel(config, add_pooling_layer=False)
        self.bert.train()

        config2 = deepcopy(config)
        config2.num_attention_heads = 6
        config2.num_hidden_layers = 4
        config2.intermediate_size = config.hidden_size * 2

        self.encoder = BertEncoder(config2)
        self.encoder.train()

        # Initialize weights and apply final processing
        self.post_init()

    def forward(
        self,
        state,
        input_ids=None,
        attention_mask=None,
        token_type_ids=None,
        bins_mask=None,
        position_ids=None,
        head_mask=None,
        inputs_embeds=None,
        labels=None,
        labels_mask=None,
        output_attentions=None,
        output_hidden_states=None,
        return_dict=None,
        positions=None
    ):
        r"""
        labels (`torch.LongTensor` of shape `(batch_size, sequence_length)`, *optional*):
            Labels for computing the token classification loss. Indices should be in `[0, ..., config.num_labels - 1]`.
        """
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        outputs = self.bert(
            input_ids,
            attention_mask=attention_mask,
            token_type_ids=token_type_ids,
            position_ids=position_ids,
            head_mask=head_mask,
            inputs_embeds=inputs_embeds,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )

        h = outputs[0]

        hs = torch.cat([h, state], dim=1)
        hs = self.encoder(hs)['last_hidden_state']
        new_state = hs[:, h.shape[1]:]
        out = hs[:, : h.shape[1]]

        empty_mask = attention_mask.type(torch.int32).sum(1)
        new_state[empty_mask == 0] = state[empty_mask == 0]

        return out, h, bins_mask, new_state
    


class MemUpMemoryImpl(MemUpMemory):

    def __init__(self, mem_tr: BertForEnformer):
        super().__init__()
        self.mem_tr = mem_tr

    def forward(self, data: Dict[str, Tensor], state: State) -> Tuple[Tensor, State]:
        out, hidden, bins_mask, new_state = self.mem_tr.forward(state, **data)

        if bins_mask is not None:
            lens = bins_mask.type(torch.int32).sum(1) / 2
            lens = lens.cpu().type(torch.int32).numpy().tolist()
            B, D = out.shape[0], out.shape[-1]
            padded_hidden = pad_sequence(torch.split(hidden[bins_mask].reshape(-1, D * 2), lens), batch_first=True)
            # print(padded_hidden.shape, data["labels"].shape, data["positions"].shape, data["input_ids"].shape)
            bins_output = torch.cat([
                data["positions"],
                pad_sequence(torch.split(hidden[bins_mask].reshape(-1, D * 2), lens), batch_first=True),
                pad_sequence(torch.split(out[bins_mask].reshape(-1, D * 2), lens), batch_first=True)
            ], dim=-1)
            return bins_output, new_state
        else:
            return None, new_state
        


class MemUpMemoryRMT(MemUpMemory):

    def __init__(self, mem_tr: RecurrentTransformerWithStateEmbedding):
        super().__init__()
        self.mem_tr = mem_tr

    def forward(self, data: Dict[str, Tensor], state: State) -> Tuple[Tensor, State]:
        bert_out = self.mem_tr.forward(data, state)
        bins_mask = data["bins_mask"] if "bins_mask" in data else None
        out, new_state = bert_out.out, bert_out.state

        if bins_mask is not None:
            lens = bins_mask.type(torch.int32).sum(1) / 2
            lens = lens.cpu().type(torch.int32).numpy().tolist()
            B, D = out.shape[0], out.shape[-1]
            bins_output = torch.cat([
                data["positions"],
                pad_sequence(torch.split(out[bins_mask].reshape(-1, D * 2), lens), batch_first=True)
            ], dim=-1)
            return bins_output, new_state
        else:
            return None, new_state
    

class DataCollectorTrain(DataCollectorAppend[Dict[str, Tensor], TOSM]):
    def apply(self, data: Dict[str, Tensor], out: TokenClassifierOutput, state: State) -> TOSM:
        return TOSM(data["labels"] if "labels" in data else None, out, state, 
                    data["labels_mask"] if "labels" in data else None)
    

class ContextCollector(DataCollectorAppend[Dict[str, Tensor], Tensor]):
    def apply(self, data:  Dict[str, Tensor], out: Tensor, state: State) -> Optional[Tuple[Tensor, Tensor]]:
        return (out.cpu(), data["labels_mask"].cpu()) if out is not None else None
    
    @override
    def result(self, cat_dims: Tuple[int] = ..., cat_keys: Tuple[str] = ...):
        context = torch.cat([c for c, _ in self.collection], 1)
        c_mask = torch.cat([m for _, m in self.collection], 1)
        B, _, D = context.shape
        context = context[c_mask].reshape(B, -1, D)
        return context
    

class Predictor(nn.Module):

    def __init__(self, bert_config):
        super().__init__()
        config2 = deepcopy(bert_config)
        config2.num_attention_heads = 4
        config2.num_hidden_layers = 2
        config2.intermediate_size = bert_config.hidden_size * 2

        self.encoder = BertEncoder(config2)
        self.config = config2

        self.head = nn.Sequential(
            nn.Dropout(0.1),
            nn.Linear(bert_config.hidden_size * 2, bert_config.hidden_size * 2),
            nn.Dropout(0.1),
            nn.ReLU(),
            nn.Linear(bert_config.hidden_size * 2, 5313),
            nn.Softplus()
        )

    def get_extended_attention_mask(
        self, attention_mask: Tensor, input_shape: Tuple[int]
    ) -> Tensor:
        
        dtype = torch.float32

        if attention_mask.dim() == 3:
            extended_attention_mask = attention_mask[:, None, :, :]
        elif attention_mask.dim() == 2:
            extended_attention_mask = attention_mask[:, None, None, :]
        else:
            raise ValueError(
                f"Wrong shape for input_ids (shape {input_shape}) or attention_mask (shape {attention_mask.shape})"
            )

        extended_attention_mask = extended_attention_mask.to(dtype=dtype)  # fp16 compatibility
        extended_attention_mask = (1.0 - extended_attention_mask) * torch.finfo(dtype).min
        return extended_attention_mask



    def forward(self, x, state, mask):
        B, D = state.shape[0], state.shape[2]
        T = x.shape[1]
        mult = x.shape[2] // D
        extended_mask = mask[:, :, None].expand(*mask.shape, mult).reshape(B, T * mult).type(torch.int32)
        state_mask = torch.ones(state.shape[:2], dtype=torch.int32, device=state.device)
        extended_mask = torch.cat([extended_mask, state_mask], dim=1)
        xs = torch.cat([x.reshape(B, T * mult, D), state], dim=1)
        extended_mask = self.get_extended_attention_mask(extended_mask, xs.shape)
        out = self.encoder.forward(xs, attention_mask=extended_mask)['last_hidden_state'][:, 1:T*mult:mult//2].reshape(B, T, D * 2)
        return self.head(out)