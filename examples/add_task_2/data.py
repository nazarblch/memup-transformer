import numpy as np

import torch
import random
from torch.utils.data import Dataset


class AddTask(Dataset):
    def __init__(self, n, size):
        self.size = size
        self.X, self.Y = self.gen_batch(n)

    def __len__(self):
        return self.X.shape[0]

    def __getitem__(self, i):
        return self.X[i], self.Y[i]

    def gen_batch(self, B):
        Y = torch.zeros(B, 1)
        X = torch.randn(B, self.size, 1)
        X0 = torch.zeros(B, self.size, 1)
        for i in range(B):
            p1 = random.randint(0, self.size - 3)
            p2 = random.randint(p1 + 1, self.size - 2)
            X0[i, p1] = 1
            X0[i, p2] = 1
            Y[i] = X[i, p1] + X[i, p2]

        X = torch.cat([X, X0], -1)

        return X, Y