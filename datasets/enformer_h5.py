import h5py
import numpy as np
from torch.utils.data import Dataset
from typing import List


class EnformerDataset(Dataset):

    def __init__(self, folds: List[str]):
        self.targets = []
        self.texts = []
        self.coords = []

        for path in folds:
            print(path)
            with h5py.File(path, "r") as f:
                for key in f.keys():
                    self.targets.append(f[key]["target"][()])
                    self.coords.append(f[key]["coordinates"][()])
                    self.texts.append(f[key]["seq"][()].decode('UTF-8'))

        self.targets = np.stack(self.targets)
        self.coords = np.stack(self.coords)

    def __len__(self):
        return len(self.texts)

    def __getitem__(self, idx):
        text = self.texts[idx]
        return text, self.targets[idx], self.coords[idx]