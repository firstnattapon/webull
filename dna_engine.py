from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable

import numpy as np


@dataclass(frozen=True)
class DnaSpec:
    length: int
    mutation_rate: float
    dna_seed: int
    mutation_seeds: tuple[int, ...]
    raw_numbers: tuple[int, ...]

    @property
    def seeds(self) -> tuple[int, ...]:
        return (self.dna_seed, *self.mutation_seeds)


def decode_number_stream(encoded: str) -> list[int]:
    """Decode [len][value][len][value]... into integers."""
    if not encoded or not encoded.isdigit():
        raise ValueError("DNA string must be a non-empty digit string")

    values: list[int] = []
    index = 0
    while index < len(encoded):
        token_width = int(encoded[index])
        index += 1
        if token_width <= 0:
            raise ValueError("DNA token width must be greater than 0")

        next_index = index + token_width
        if next_index > len(encoded):
            raise ValueError("DNA string ended before a full token was decoded")

        token = encoded[index:next_index]
        values.append(int(token))
        index = next_index

    return values


def normalize_mutation_rate(raw_rate: int | float) -> float:
    """Interpret 10 as 10%, while preserving literal probabilities <= 1."""
    rate = float(raw_rate)
    if rate < 0:
        raise ValueError("DNA mutation rate cannot be negative")
    if rate > 1:
        rate /= 100.0
    if rate > 1:
        raise ValueError("DNA mutation rate cannot be greater than 100%")
    return rate


def parse_dna_spec(encoded: str) -> DnaSpec:
    numbers = decode_number_stream(encoded)
    if len(numbers) < 3:
        raise ValueError("DNA string must encode length, rate, and at least one seed")

    length = int(numbers[0])
    if length <= 0:
        raise ValueError("DNA length must be greater than 0")

    return DnaSpec(
        length=length,
        mutation_rate=normalize_mutation_rate(numbers[1]),
        dna_seed=int(numbers[2]),
        mutation_seeds=tuple(int(seed) for seed in numbers[3:]),
        raw_numbers=tuple(numbers),
    )


def parse_bypass_dna_code(encoded: str) -> int | None:
    """Return the bypass length when encoded requests an all-ones DNA array."""
    text = encoded.strip()
    if text.lower().startswith("bypass:"):
        raw_length = text.split(":", 1)[1].strip()
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError("Bypass DNA length must be an integer") from exc
        if length <= 0:
            raise ValueError("Bypass DNA length must be greater than 0")
        return length

    if text.startswith("["):
        try:
            values = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("Bypass DNA array format must be [1, length]") from exc
        if (
            not isinstance(values, list)
            or len(values) != 2
            or type(values[0]) is not int
            or type(values[1]) is not int
        ):
            raise ValueError("Bypass DNA array format must be [1, length]")
        if values[0] != 1:
            raise ValueError("Bypass DNA array only supports value 1")
        if values[1] <= 0:
            raise ValueError("Bypass DNA length must be greater than 0")
        return values[1]

    return None


def decode_dna(encoded: str) -> np.ndarray:
    """Build the Hybrid Multi-Mutation action array from an encoded DNA string."""
    bypass_length = parse_bypass_dna_code(encoded)
    if bypass_length is not None:
        return np.ones(bypass_length, dtype=np.int8)

    spec = parse_dna_spec(encoded)
    rng = np.random.default_rng(seed=spec.dna_seed)
    dna = rng.integers(0, 2, size=spec.length).astype(np.int8)
    dna[0] = 1

    for seed in spec.mutation_seeds:
        mutation_rng = np.random.default_rng(seed=seed)
        mutation_mask = mutation_rng.random(spec.length) < spec.mutation_rate
        dna[mutation_mask] = 1 - dna[mutation_mask]
        dna[0] = 1

    return dna


def encode_dna(length: int, mutation_rate: int, seeds: Iterable[int]) -> str:
    """Encode parameters into the compact [len][value] stream."""
    values = [int(length), int(mutation_rate), *[int(seed) for seed in seeds]]
    if len(values) < 3:
        raise ValueError("At least one seed is required")
    if values[0] <= 0:
        raise ValueError("length must be greater than 0")

    return "".join(f"{len(str(value))}{value}" for value in values)
