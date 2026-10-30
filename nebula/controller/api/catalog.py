"""Catálogo público de Nebula: valores válidos para describir un experimento."""
from typing import Literal

Federation = Literal["DFL", "SDFL", "CFL"]
Topology = Literal["Fully", "Ring", "Star", "Random"]
Aggregator = Literal["FedAvg", "Krum", "Median", "TrimmedMean"]
ExperimentStatus = Literal["draft", "queued", "running", "finished", "failed"]

# dataset -> modelos disponibles (según nebula/core/models/*)
DATASET_MODELS: dict[str, list[str]] = {
    "MNIST": ["MLP", "CNN"],
    "FashionMNIST": ["MLP", "CNN"],
    "EMNIST": ["MLP", "CNN"],
    "CIFAR10": ["CNN", "CNNv2", "CNNv3", "ResNet", "FasterMobileNet", "SimpleMobileNet"],
    "CIFAR100": ["CNN"],
    "Sentiment140": ["CNN", "RNN"],
    "KDDCUP99": ["MLP"],
}
