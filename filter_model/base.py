from abc import ABC, abstractmethod
from typing import Callable, Optional, Iterator

import torch
from torch import nn, Tensor

from models.stoch_tensor import StochasticBinaryTensor
from models.transformers import RecurrentTransformer, RecurrentOutputSeq


class FilterModel(nn.Module, ABC):
    @abstractmethod
    def forward(self, data: Tensor) -> Callable[[Tensor], Optional[Tensor]]:
        pass


class NStepFilter(FilterModel):

    def __init__(self, steps: int, model: FilterModel):
        super().__init__()
        self.steps = steps
        self.model = model

    def forward(self, data: Tensor) -> Callable[[Tensor], Optional[Tensor]]:
        proc_state = self.model(data)
        n = [0]

        def new_proc_state(state: Tensor):
            if n[0] < self.steps:
                res = proc_state(state)
            else:
                res = None
            n[0] += 1

            return res

        return new_proc_state


class NStepFilterObject:

    def __init__(self, steps: int):
        super().__init__()
        self.steps = steps

    def __call__(self, model: FilterModel) -> NStepFilter:
        self.model = model
        return NStepFilter(self.steps, self.model)


class FilteredRecurrentTransformer(RecurrentTransformer):

    def __init__(self,
                 transformer: RecurrentTransformer,
                 filter_model: FilterModel,
                 rollout: int,
                 embedding: Optional[nn.Module] = None):
        super().__init__()
        self.transformer = transformer
        self.filter_model = filter_model
        self.steps = rollout
        self.embedding = embedding
        self.state_filter = nn.Sequential(
            nn.Linear(768 * 2, 768),
            nn.ReLU(),
            nn.Linear(768, 2)
        )

    def forward(self, data: Tensor, s: Tensor) -> Iterator[RecurrentOutputSeq]:

        if self.embedding is not None:
            data = self.embedding(data)
        proc_state = self.filter_model(data)

        step = 1
        fd, mask = proc_state(s)
        s_seq = RecurrentOutputSeq()

        while fd is not None:

            os = self.transformer.forward(fd, s)
            # m: Tensor = self.state_filter(torch.cat([s, s1], dim=-1)).softmax(-1)
            s_seq.append(os, mask)
            # s = m[:, :, 0][:, :, None] * s + m[:, :, 1][:, :, None] * s1
            s = os.state

            if step % self.steps == 0 and step > 0:
                yield s_seq
                s = s.detach()
                s_seq = RecurrentOutputSeq()

            fd, mask = proc_state(s)

            if fd is None and step % self.steps != 0 and step > 0:
                yield s_seq

            step += 1
