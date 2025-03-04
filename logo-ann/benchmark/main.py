import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import time
from typing import Callable, Dict, List, Optional, Set, Tuple

import numpy as np
from PIL import Image
import timm
from timm.data import resolve_data_config
from timm.data.transforms_factory import create_transform
import torch
from torch.utils.data import DataLoader, Dataset
import tqdm
from transformers import CLIPModel, CLIPProcessor


MODEL_NAMES = [
    "efficientnet_b0",
    "efficientnet_b1",
    "efficientnet_b2",
    "efficientnet_b4",
    "beit_base_patch16_384",
    "beit_large_patch16_224_in22k",
    "beit_large_patch16_384",
    "clip-vit-base-patch16",
    "clip-vit-base-patch32",
    "clip-vit-large-patch14",
    "deit_base_patch16_384",
    "resnest101e",
    "resnet50",
    "resnet50d",
    "rexnet_100",
    "random",
]


class LogoDataset(Dataset):
    def __init__(
        self,
        root_dir: Path,
        transform_func: Callable[[Image.Image], Image.Image],
        split_set: Optional[Set[str]] = None,
    ) -> None:
        super().__init__()
        self.root_dir = root_dir
        self.transform_func = transform_func
        children = [p for p in root_dir.iterdir() if p.is_dir()]
        self.label_names = []
        self.image_paths = []
        labels_by_name = []
        for child in children:
            image_paths = list(child.glob("*.png"))
            if split_set is not None:
                image_paths = [
                    x for x in image_paths if f"{x.parent.name}/{x.name}" in split_set
                ]
            if image_paths:
                self.image_paths += image_paths
                labels_by_name += [child.name] * len(image_paths)

        self.label_names = sorted(set(labels_by_name))
        self.labels = [self.label_names.index(name) for name in labels_by_name]

    def __getitem__(self, idx: int):
        image = Image.open(self.image_paths[idx])
        return (self.transform_func(image), self.labels[idx])

    def __len__(self):
        return len(self.image_paths)


def generate_embeddings(
    model: torch.nn.Module, data_loader: DataLoader, device: torch.device
) -> Tuple[torch.Tensor, torch.Tensor, float]:
    model.eval()
    embedding_all = []
    labels_all = []
    elapsed = 0.0

    with torch.inference_mode():
        for inputs, labels in tqdm.tqdm(data_loader):
            start_time = time.monotonic()
            output = model(inputs.to(device))
            elapsed += time.monotonic() - start_time
            embedding_all.append(output)
            labels_all.append(labels)

        return torch.cat(embedding_all), torch.cat(labels_all), elapsed


def generate_embeddings_clip(
    model: torch.nn.Module, data_loader: DataLoader, device: torch.device
) -> Tuple[torch.Tensor, torch.Tensor, float]:
    model.eval()
    embedding_all = []
    labels_all = []
    elapsed = 0.0

    with torch.inference_mode():
        for inputs, labels in tqdm.tqdm(data_loader):
            start_time = time.monotonic()
            outputs = model(**{"pixel_values": inputs.to(device)})
            elapsed += time.monotonic() - start_time
            embedding_all.append(outputs.pooler_output)
            labels_all.append(labels)

        return torch.cat(embedding_all), torch.cat(labels_all), elapsed


def pairwise_squared_euclidian_distance_numpy(A: np.ndarray) -> np.ndarray:
    assert len(A.shape) == 2
    dot_product = np.dot(A[:, None, :], A[None, :, :].swapaxes(1, 2)).squeeze()
    squared_sum = np.sum(A ** 2.0, axis=1, keepdims=True)
    return squared_sum + squared_sum.transpose() - 2 * dot_product


def pairwise_squared_euclidian_distance(A: np.ndarray) -> torch.Tensor:
    assert len(A.shape) == 2
    dot_product = torch.matmul(A[:, None, :], A[None, :, :].swapaxes(1, 2)).squeeze()
    squared_sum = torch.sum(A ** 2.0, axis=1, keepdim=True)
    return squared_sum + squared_sum.T - 2 * dot_product


def run_model(
    root_dir: Path,
    split_set: Optional[Set[str]],
    model_name: str,
    batch_size: int,
    device: torch.device,
) -> Tuple[torch.Tensor, torch.Tensor, LogoDataset, float]:
    model = timm.create_model(model_name, pretrained=True, num_classes=0)
    model.to(device)
    config = resolve_data_config({}, model=model)
    transform_func = create_transform(**config)
    dataset = LogoDataset(root_dir, transform_func, split_set)
    data_loader = DataLoader(dataset, batch_size, num_workers=2)
    embeddings, labels, elapsed = generate_embeddings(model, data_loader, device)
    return embeddings, labels, dataset, elapsed


def run_clip_model(
    root_dir: Path,
    split_set: Optional[Set[str]],
    model_name: str,
    batch_size: int,
    device: torch.device,
):
    model = CLIPModel.from_pretrained(f"openai/{model_name}").vision_model
    model.to(device)
    processor = CLIPProcessor.from_pretrained(f"openai/{model_name}")
    transform_func = lambda x: processor(images=x, return_tensors="pt")["pixel_values"][
        0
    ]
    dataset = LogoDataset(root_dir, transform_func, split_set)
    data_loader = DataLoader(dataset, batch_size, num_workers=2)
    embeddings, labels, elapsed = generate_embeddings_clip(model, data_loader, device)
    return embeddings, labels, dataset, elapsed


def compute_metrics(
    distance_matrix: np.ndarray,
    labels: np.ndarray,
    k_list: List[int],
    max_label_count: int = 5,
):
    mask = get_distance_matrix_mask(
        distance_matrix.shape[0], max_label_count=max_label_count, labels=labels,
    )
    return compute_metrics_k(distance_matrix, mask, labels, k_list, max_label_count)


def get_distance_matrix_mask(
    size: int, max_label_count: int = 0, labels: Optional[np.ndarray] = None,
) -> np.ndarray:
    mask = np.ones((size, size), dtype=bool)

    if max_label_count:
        if labels is None:
            raise ValueError("labels must be provided if max_label_count != 0")

        unique_labels = set(map(int, labels))
        for row_idx in range(size):
            query_label = labels[row_idx]
            for label in unique_labels:
                population = np.where(labels == label)[0]
                if label == query_label:
                    population = population[population != row_idx]
                column_indices = np.random.choice(
                    population,
                    size=min(len(population), max_label_count),
                    replace=False,
                )
                mask[row_idx, column_indices] = False

    return mask


def compute_metrics_k(
    distance_matrix: np.ndarray,
    mask: np.ndarray,
    labels: np.ndarray,
    k_list: List[int],
    max_label_count: int,
):
    label_count = Counter(map(int, labels))
    n_labels = len(set(labels))
    assert sorted(label_count) == list(range(labels.max() + 1))
    results = {
        "precision": defaultdict(lambda: np.zeros(n_labels)),
        "recall": defaultdict(lambda: np.zeros(n_labels)),
        "micro_precision": {},
        "macro_precision": {},
        "micro_recall": {},
        "macro_recall": {},
    }
    distance_matrix = distance_matrix.copy()
    distance_matrix[mask] = np.inf
    max_k = max(k_list)
    sort_indices = np.argsort(distance_matrix, axis=1)[:, :max_k]
    top_k_labels = labels[sort_indices]
    # matches: (sample, max_k)
    matches = (top_k_labels == labels[:, None]).astype(int)
    print(f"label count: {label_count}")
    for k in k_list:
        tp_sum = 0
        tp_fp_sum = 0
        tp_fn_sum = 0
        # matches_k: (samples, )
        matches_k = matches[:, :k].sum(axis=1)
        for label in label_count:
            # tp: (number of true positive for label, )
            tp = matches_k[np.where(labels == label)[0]]
            print(f"tp for label {label}: {tp}")
            positive_all = tp.shape[0]
            print(f"positive_all for label {label}: {positive_all}")
            tp_fp = k * positive_all
            print(f"tp_fp for label {label}: {tp_fp}")
            tp_fn = max_label_count * positive_all
            results["precision"][k][label] = tp.sum() / tp_fp
            results["recall"][k][label] = tp.sum() / tp_fn
            tp_sum += tp.sum()
            tp_fp_sum += tp_fp
            tp_fn_sum += tp_fn

        results["micro_precision"][k] = tp_sum / tp_fp_sum
        results["micro_recall"][k] = tp_sum / tp_fn_sum
        results["macro_precision"][k] = float(results["precision"][k].mean())
        results["macro_recall"][k] = float(results["recall"][k].mean())
    return results


def save_metrics(metrics, dataset, model_name: str):
    OUTPUT_DIR = Path(f"results/{model_name}")
    OUTPUT_DIR.mkdir(exist_ok=True, parents=True)

    with (OUTPUT_DIR / "recall.txt").open("w") as f:
        for k, values in metrics["recall"].items():
            print(f"  k = {k}", file=f)
            assert len(values) == len(dataset.label_names)
            for value, label_name in zip(values, dataset.label_names):
                print(f"    {label_name}: {value}", file=f)

    with (OUTPUT_DIR / "precision.txt").open("w") as f:
        for k, values in metrics["precision"].items():
            print(f"  k = {k}", file=f)
            assert len(values) == len(dataset.label_names)
            for value, label_name in zip(values, dataset.label_names):
                print(f"    {label_name}: {value}", file=f)

    with (OUTPUT_DIR / "all_metrics.txt").open("w") as f:
        for metric in (
            "micro_recall",
            "macro_recall",
            "micro_precision",
            "macro_precision",
        ):
            print(f"--- {metric} ---", file=f)
            for k, value in metrics[metric].items():
                print(f"  k = {k}:    {value}", file=f)


def evaluate():
    with open("val.txt", "r") as f:
        split_set = set(map(str.strip, f))

    batch_size = 8
    k_list = [1, 2, 4, 10, 100]
    max_label_count = 4
    for model_name in MODEL_NAMES:
        if model_name == "random":
            distance_matrix = np.random.rand(6367, 6367)
        else:
            run_func = run_clip_model if model_name.startswith("clip") else run_model
            embeddings, labels, dataset, _ = run_func(
                root_dir=Path("logo_dataset"),
                split_set=split_set,
                model_name=model_name,
                batch_size=batch_size,
                device=torch.device("cuda:0"),
            )
            # print(f"Embedding shape: {embeddings.shape}")
            # print(f"Labels shape: {labels.shape}")
            with torch.inference_mode():
                distance_matrix = (
                    pairwise_squared_euclidian_distance(embeddings).cpu().numpy()
                )

        metrics = compute_metrics(
            distance_matrix,
            labels.cpu().numpy(),
            k_list=k_list,
            max_label_count=max_label_count,
        )
        save_metrics(metrics, dataset, model_name)


def run_latency_benchmark(device: torch.device):
    with open("val.txt", "r") as f:
        split_set = set(map(str.strip, f))

    batch_size = 8
    results: Dict[str, Dict] = {}
    n = batch_size * 10
    split_set = set(list(split_set)[:n])
    for model_name in MODEL_NAMES:
        results[model_name] = {}
        if model_name == "random":
            continue
        run_func = run_clip_model if model_name.startswith("clip") else run_model
        embeddings, _, __, elapsed = run_func(
            root_dir=Path("logo_dataset"),
            split_set=split_set,
            model_name=model_name,
            batch_size=batch_size,
            device=device,
        )
        results[model_name]["elapsed_total"] = elapsed
        results[model_name]["elapsed_per_sample"] = elapsed / n
        results[model_name]["embedding_size"] = int(embeddings.shape[0])
        print(
            f"{model_name}, elapsed_total: {elapsed} (s), elapsed_per_sample: {elapsed/n}"
        )
        print(f"{model_name}: {embeddings.shape}")

    with open("latency_benchmark.json", "w") as f:
        json.dump(results, f)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="subparser_name")
    evaluate_parser = subparsers.add_parser("evaluate", help="Launch evaluation")
    benchmark_parser = subparsers.add_parser(
        "benchmark", help="Launch latency benchmark"
    )
    benchmark_parser.add_argument("--gpu", action="store_true")
    args = parser.parse_args()

    if args.subparser_name == "evaluate":
        evaluate()
    elif args.subparser_name == "benchmark":
        run_latency_benchmark(torch.device("cuda:0" if args.gpu else "cpu"))
