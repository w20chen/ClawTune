"""Torch quantile MLP used by the tabular resource evaluator."""

from __future__ import annotations

from dataclasses import dataclass

import torch
from torch import nn


@dataclass(frozen=True)
class MLPConfig:
    """Architecture and output definition for a multi-target quantile MLP."""

    target_names: tuple[str, ...]
    quantiles: tuple[float, ...] = (0.5, 0.9, 0.99)
    hidden_dim: int = 256
    dropout: float = 0.1


def _head(in_dim: int, config: MLPConfig) -> nn.Sequential:
    return nn.Sequential(
        nn.Linear(in_dim, config.hidden_dim),
        nn.GELU(),
        nn.Dropout(config.dropout),
        nn.Linear(config.hidden_dim, config.hidden_dim),
        nn.GELU(),
        nn.Dropout(config.dropout),
        nn.Linear(config.hidden_dim, len(config.quantiles)),
    )


class QuantileMLP(nn.Module):
    """Independent quantile heads over one shared numeric feature matrix."""

    def __init__(self, in_dim: int, config: MLPConfig) -> None:
        super().__init__()
        self.config = config
        self.register_buffer(
            "_quantiles",
            torch.tensor(config.quantiles, dtype=torch.float32),
        )
        self.heads = nn.ModuleDict(
            {name: _head(in_dim, config) for name in config.target_names}
        )

    def forward(self, features: torch.Tensor) -> dict[str, torch.Tensor]:
        return {name: head(features) for name, head in self.heads.items()}

    def loss(
        self,
        predictions: dict[str, torch.Tensor],
        targets: dict[str, torch.Tensor],
        masks: dict[str, torch.Tensor],
    ) -> torch.Tensor:
        """Mean pinball loss over targets represented in the batch."""

        quantiles = self._quantiles.view(1, -1)
        total = torch.zeros((), device=self._quantiles.device)
        count = 0
        for name, predicted in predictions.items():
            mask = masks[name]
            if not bool(mask.any()):
                continue
            error = targets[name][mask].unsqueeze(1) - predicted[mask]
            total = total + torch.maximum(
                quantiles * error,
                (quantiles - 1.0) * error,
            ).mean()
            count += 1
        return total / max(count, 1)


def train_quantile_mlp(
    model: QuantileMLP,
    features: torch.Tensor,
    targets: dict[str, torch.Tensor],
    masks: dict[str, torch.Tensor],
    *,
    epochs: int,
    learning_rate: float,
    batch_size: int,
) -> None:
    """Train quantile heads on an in-memory numeric feature matrix."""

    if epochs == 0:
        return
    parameters = list(model.parameters())
    optimizer = torch.optim.AdamW(parameters, lr=learning_rate)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
        optimizer,
        T_max=epochs,
    )
    model.train()
    for _ in range(epochs):
        permutation = torch.randperm(len(features), device=features.device)
        for start in range(0, len(features), batch_size):
            indices = permutation[start : start + batch_size]
            loss = model.loss(
                model(features[indices]),
                {name: values[indices] for name, values in targets.items()},
                {name: values[indices] for name, values in masks.items()},
            )
            if not loss.requires_grad:
                continue
            optimizer.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(parameters, 1.0)
            optimizer.step()
        scheduler.step()


__all__ = ["MLPConfig", "QuantileMLP", "train_quantile_mlp"]
