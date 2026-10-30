"""Shared model registry (Sprint 5, ROADMAP_AGENTE_ADAPTATIVO).

Single source of truth for (dataset, model-name) -> model class, mirroring
``nebula/core/node.py`` (which keeps its own hardcoded mapping; this module
does not refactor it, only mirrors it for the swap path and the cost model).

Compatibility matrix (documented per Sprint 5 task 3):

| Change | Same I/O? | Same state_dict? | Continuity |
|---|---|---|---|
| Same dataset, same model | yes | yes | full (reload) |
| Same dataset, other model | yes (same input tensor + num classes) | NO (different layers) | none: fresh weights, federation continues (Sprint 6 adds partial reuse) |
| Other dataset | NO | NO | none: rejected for swap (needs new execution) |

Same-dataset interchangeability holds because every model consumes the
dataset's canonical input tensor and emits ``[B, num_classes]`` logits with
``CrossEntropyLoss``; architectures (MLP vs CNN vs ResNet vs MobileNet)
differ internally, so weights are never portable across names.
"""

from __future__ import annotations

import logging

#: (DATASET, MODEL) -> (module, class, kwargs), mirroring node.py.
MODEL_REGISTRY: dict[tuple[str, str], tuple[str, str, dict]] = {
    ("MNIST", "MLP"): ("nebula.core.models.mnist.mlp", "MNISTModelMLP", {}),
    ("MNIST", "CNN"): ("nebula.core.models.mnist.cnn", "MNISTModelCNN", {}),
    ("FASHIONMNIST", "MLP"): ("nebula.core.models.fashionmnist.mlp", "FashionMNISTModelMLP", {}),
    ("FASHIONMNIST", "CNN"): ("nebula.core.models.fashionmnist.cnn", "FashionMNISTModelCNN", {}),
    ("EMNIST", "MLP"): ("nebula.core.models.emnist.mlp", "EMNISTModelMLP", {}),
    ("EMNIST", "CNN"): ("nebula.core.models.emnist.cnn", "EMNISTModelCNN", {}),
    ("COVTYPE", "MLP"): ("nebula.core.models.covtype.mlp", "CovtypeModelMLP", {}),
    ("KDDCUP99", "MLP"): ("nebula.core.models.kddcup99.mlp", "KDDCUP99ModelMLP", {}),
    ("ADULTCENSUS", "MLP"): ("nebula.core.models.adultcensus.mlp", "AdultCensusModelMLP", {}),
    ("BREASTCANCER", "MLP"): ("nebula.core.models.breast_cancer.mlp", "BreastCancerModelMLP", {}),
    ("CIFAR10", "RESNET9"): ("nebula.core.models.cifar10.resnet", "CIFAR10ModelResNet", {"classifier": "resnet9"}),
    ("CIFAR10", "FASTERMOBILENET"): ("nebula.core.models.cifar10.fastermobilenet", "FasterMobileNet", {}),
    ("CIFAR10", "SIMPLEMOBILENET"): ("nebula.core.models.cifar10.simplemobilenet", "SimpleMobileNetV1", {}),
    ("CIFAR10", "CNN"): ("nebula.core.models.cifar10.cnn", "CIFAR10ModelCNN", {}),
    ("CIFAR10", "CNNV2"): ("nebula.core.models.cifar10.cnnV2", "CIFAR10ModelCNN_V2", {}),
    ("CIFAR10", "CNNV3"): ("nebula.core.models.cifar10.cnnV3", "CIFAR10ModelCNN_V3", {}),
    ("CIFAR100", "CNN"): ("nebula.core.models.cifar100.cnn", "CIFAR100ModelCNN", {}),
}


def compatible_models(dataset: object) -> list[str]:
    """Model names supported for a dataset (swap candidates). Never raises."""
    try:
        ds = str(dataset).upper()
        return sorted(name for (d, name) in MODEL_REGISTRY if d == ds)
    except Exception:
        return []


def instantiate(dataset: object, model_name: object):
    """Instantiate a registered model with defaults on CPU (no data, no GPU).

    Raises KeyError for unknown pairs and propagates constructor errors
    (callers map them to clear rejection messages). Heavy imports stay here.
    """
    key = (str(dataset).upper(), str(model_name).upper())
    entry = MODEL_REGISTRY.get(key)
    if entry is None:
        raise KeyError(f"model {model_name!r} not supported for dataset {dataset!r}")
    module_path, class_name, kwargs = entry
    import importlib

    mod = importlib.import_module(module_path)
    cls = getattr(mod, class_name)
    return cls(**kwargs)


def check_compatible(dataset: object, from_model: object, to_model: object) -> tuple[bool, str]:
    """Validate a swap candidate. Returns (ok, message). Never raises."""
    try:
        candidates = compatible_models(dataset)
        if not candidates:
            return False, f"dataset {dataset!r} not supported (no registered models)"
        if str(to_model).upper() not in candidates:
            return False, (f"model {to_model!r} incompatible with dataset {dataset!r}: "
                           f"compatible models: {candidates}")
        if str(from_model).upper() == str(to_model).upper():
            return False, f"already using {to_model!r} (no-op swap rejected)"
        return True, f"{from_model!r} -> {to_model!r} compatible (same I/O, fresh weights)"
    except Exception as e:
        logging.warning(f"models-registry | compat check failed: {e}")
        return False, f"compat check failed: {e}"
