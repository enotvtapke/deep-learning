import h5py
import numpy as np
import os
from PIL import Image
from PIL.Image import Image as Image_t
import matplotlib.pyplot as plt
import torch

from abc import ABC, abstractmethod
from typing import Type, Union


class storage_class(ABC):
    def __init__(self, config: dict):
        self.config = config
        self.pos = 0
        self.__locked = False
        
    @property
    @abstractmethod
    def dataset_size(self) -> int:
        """
        Return the number of pairs (image, segmentation mask) in the dataset
        """
        pass
    
    @abstractmethod
    def append(self, input_chunk: np.array, target_chunk: np.array) -> None:
        """
        Add a block of images `input_chunk` and a block of masks `target_chunk` to the dataset
        """
        pass
    
    @abstractmethod
    def lock(self) -> None:
        """
        Close write mode, open in read mode
        """
        pass
    
    @abstractmethod
    def __getitem__(self, idx: int) -> tuple[Image_t, Image_t]:
        """
        Return pair (image, mask) at index `idx`
        """
        pass

    
class storage_hdf5(storage_class):
    def __init__(self, config):
        super().__init__(config)
        
        self.dataset = h5py.File(f'{config["annotation_file"].rstrip(".txt")}.h5', 'a')
        try:
            self.dataset.create_dataset("input", shape=(config["dataset_size"], *config["target_shape"], 3), dtype=np.uint8)
        except ValueError:
            pass
        try:
            self.dataset.create_dataset("target",  shape=(config["dataset_size"], *config["target_shape"]), dtype=np.uint8)
        except ValueError:
            pass

    @property
    def dataset_size(self):
        assert self.dataset["input"].shape[:-1] == self.dataset["target"].shape
        assert self.dataset["target"].shape[0] == self.config["dataset_size"]
        return self.config["dataset_size"]
    
    def append(self, input_chunk, target_chunk):
        l_idx = self.pos * self.config["chunk_size"]
        r_idx = min(self.config["dataset_size"], l_idx + self.config["chunk_size"])
        self.dataset["input"][l_idx:r_idx] = input_chunk[:r_idx-l_idx]
        self.dataset["target"][l_idx:r_idx] = target_chunk[:r_idx-l_idx]
        self.pos += 1
    
    def lock(self):
        self.dataset.close()
        self.dataset = h5py.File(f'{self.config["annotation_file"].rstrip(".txt")}.h5', 'r')
        self.__locked = True
    
    def __getitem__(self, idx):
        if not(self.__locked):
            raise AttributeError("HDF5 file is not locked. Access denied.")
        return Image.fromarray(self.dataset["input"][idx]), Image.fromarray(self.dataset["target"][idx])

    
class storage_memmap(storage_class):
    def __init__(self, config):
        super().__init__(config)
        
        path = config["annotation_file"].rstrip(".txt")
        os.makedirs(path, exist_ok=True)
        self.input_path = os.path.join(path, "input.npy")
        self.target_path = os.path.join(path, "target.npy")

        self.input = np.memmap(
            self.input_path, mode='w+', dtype=np.uint8,
            shape=(config["dataset_size"], *config["target_shape"], 3)
        )
        self.target = np.memmap(
            self.target_path, mode='w+', dtype=np.uint8,
            shape=(config["dataset_size"], *config["target_shape"])
        )
    
    @property
    def dataset_size(self):
        assert self.input.shape[:-1] == self.target.shape
        assert self.input.shape[0] == self.config["dataset_size"]
        return self.config["dataset_size"]
                                 
    def append(self, input_chunk, target_chunk):
        l_idx = self.pos * self.config["chunk_size"]
        r_idx = min(self.config["dataset_size"], l_idx + self.config["chunk_size"])
        self.input[l_idx:r_idx] = input_chunk[:r_idx-l_idx]
        self.target[l_idx:r_idx] = target_chunk[:r_idx-l_idx]
        self.pos += 1
    
    def lock(self):
        del self.input
        del self.target
        self.input = np.memmap(
            self.input_path, mode='r', dtype=np.uint8,
            shape=(self.config["dataset_size"], *self.config["target_shape"], 3)
        )
        self.target = np.memmap(
            self.target_path, mode='r', dtype=np.uint8,
            shape=(self.config["dataset_size"], *self.config["target_shape"])
        )
        self.__locked = True
    
    def __getitem__(self, idx):
        if not(self.__locked):
            raise AttributeError("Memory-mapped file is not locked. Access denied.")
        return Image.fromarray(self.input[idx]), Image.fromarray(self.target[idx])

    
class storage_raw(storage_class):
    def __init__(self, config):
        super().__init__(config)
                                 
        path = config["annotation_file"].rstrip(".txt")
        self.input_path = os.path.join(path, "Input")
        self.target_path = os.path.join(path, "Target")
        self.idx2input = lambda idx: os.path.join(self.input_path, f"input{idx}.jpg")
        self.idx2target = lambda idx: os.path.join(self.target_path, f"target{idx}.png")
        
        os.makedirs(path, exist_ok=True)
        os.makedirs(self.input_path, exist_ok=False)
        os.makedirs(self.target_path, exist_ok=False)
    
    @property
    def dataset_size(self):
        assert len(os.listdir(self.input_path)) == len(os.listdir(self.target_path))
        assert len(os.listdir(self.input_path)) == self.config["dataset_size"]
        return self.config["dataset_size"]
                                 
    def append(self, input_chunk, target_chunk):
        l_idx = self.pos * self.config["chunk_size"]
        r_idx = min(self.config["dataset_size"], l_idx + self.config["chunk_size"])
                                 
        for idx in range(l_idx, r_idx):
            Image.fromarray(input_chunk[idx-l_idx], "RGB").save(self.idx2input(idx))
            Image.fromarray(target_chunk[idx-l_idx], 'L').save(self.idx2target(idx))

        self.pos += 1
                                 
    def lock(self):
        self.__locked = True
                                 
    def __getitem__(self, idx):
        if not(self.__locked):
            raise AttributeError("Raw file is not locked. Access denied.")
        input = Image.open(self.idx2input(idx)).convert("RGB")
        target = Image.open(self.idx2target(idx)).convert('L')
        return input, target


def renumerate_target(target: np.array, label: int) -> np.array:
    """
    Renumber the segmentation masks.
    In the original dataset the following labels are `{1: object, 2: background, 3: ignore_index}`,
    where object is determined by the value of the variable `label` from the set {1(cat), 2(dog)}.
    We cast the values to a single format {0: background, 1: cat, 2: dog, 255: ignore_index}.
    `ignore_index` corresponds to mask pixels that are not included in the calculation of
    quality metrics (these are the outlines of objects that are difficult to accurately predict)
    """
    target_map = {1: label, 2: 0, 3: 255}
    indexer = np.array(list(target_map.values()))
    target = indexer[(target - target.min())]
    return target


def colorize(data):
    color_map = {
        0: np.array([0, 0, 0]),  # black
        1: np.array([0, 0, 255]),  # blue
        2: np.array([255, 0, 0]),  # red
        255: np.array([255, 255, 255])  # white
    }
            
    return np.array([color_map[pixel] for pixel in data.ravel()]).reshape(*data.shape, 3)


# Function for drawing pictures
def draw(pair: tuple[torch.tensor, torch.tensor], t_dict: dict, prediction: torch.tensor = None, log: bool = False):
    """
    `pair` contains a pair (image, segmentation mask)
    `prediction` contains the segmentation mask predicted by the model
    `log` flag for requiring logging in tensorboard
    """
    input, target = pair
    num_images = 3
    if (prediction is None):
        num_images = 2
    
    f, ax = plt.subplots(1, num_images, figsize=(8*num_images, 8))
    ax[0].imshow(t_dict["backward_input"](input))
    ax[0].axis('off')
    ax[1].imshow(colorize(t_dict["backward_target"](target)))
    ax[1].axis('off')

    if not(prediction is None):
        prediction[target == 255] = 255
        ax[2].imshow(colorize(t_dict["backward_target"](prediction)))
        ax[2].axis('off')
        
    plt.tight_layout()
    if (log):
        return f
    plt.show()
