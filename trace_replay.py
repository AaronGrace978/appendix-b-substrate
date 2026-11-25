"""Replay observable event annotations without claiming access to model thoughts."""
from dataclasses import dataclass, asdict
from typing import Iterable
import json

from behavioral import Dynamics


@dataclass(frozen=True)
class Observation:
    source: str
    locator: str
    event: str
    note: str

    def __post_init__(self):
        if any(type(x) is not str or not x.strip() for x in (self.source, self.locator, self.event, self.note)):
            raise ValueError("source, locator, event, and note are required")
        if self.event not in ("supported", "refuted", "inconclusive", "duplicate"):
            raise ValueError("unsupported event")


def replay(observations: Iterable[Observation], initial: Dynamics | None = None) -> list[dict]:
    state = initial if initial is not None else Dynamics()
    trajectory = []
    for observation in observations:
        after = state.observe(observation.event)
        trajectory.append({"observation": asdict(observation), "before": asdict(state), "after": asdict(after)})
        state = after
    return trajectory


def read_jsonl(path: str) -> list[Observation]:
    with open(path, encoding="utf-8") as stream:
        return [Observation(**json.loads(line)) for line in stream if line.strip()]


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("observations")
    args = parser.parse_args()
    print(json.dumps(replay(read_jsonl(args.observations)), indent=2))
