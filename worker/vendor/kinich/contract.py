"""Contrato estandarizado de modelo — todo lo demás deriva de aquí.

Un modelo de CV se integra a Kinich implementando `CVModel` (y `StreamSession`
si declara STREAM/CLIP) y registrándose en `kinich/models/__init__.py`.
Los modelos nunca ven HTTP, bytes ni Redis: solo frames numpy y eventos Pydantic.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, ClassVar, Iterable

import numpy as np
from pydantic import BaseModel, Field


class Capability(str, Enum):
    IMAGE = "image"    # frame único -> detecciones (sync)
    CLIP = "clip"      # video corto -> eventos (job async)
    STREAM = "stream"  # RTSP -> eventos continuos


class Detection(BaseModel):
    label: str
    score: float
    bbox: tuple[int, int, int, int] | None = None  # x1, y1, x2, y2 en píxeles


class Event(BaseModel):
    type: str                                # p.ej. "FUEGO", "HUMO", "FIGHT"
    score: float
    ts: float                                # timestamp del medio (segundos)
    detections: list[Detection] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)


class ModelInfo(BaseModel):
    name: str                                # id url-safe, p.ej. "fire-detector"
    version: str
    task: str                                # descripción humana
    capabilities: set[Capability]
    event_types: list[str] = Field(default_factory=list)
    config_schema: dict[str, Any] = Field(default_factory=dict)  # JSON Schema de knobs por sesión
    target_fps: float = 5.0                  # fps que el modelo quiere consumir de un stream


class StreamSession(ABC):
    """Instancia con estado por stream: buffers temporales (MOG2, flicker,
    deques de suavizado). Los pesos viven en el modelo padre y se comparten."""

    @abstractmethod
    def process_frame(self, frame: np.ndarray, ts: float) -> list[Event]: ...

    def close(self) -> None:  # libera estado por sesión (no los pesos)
        pass


class CVModel(ABC):
    info: ClassVar[ModelInfo]

    def load(self) -> None:  # pesos -> VRAM; se llama una vez, en thread worker
        pass

    def warmup(self) -> None:  # inferencia dummy para inicializar CUDA
        pass

    def unload(self) -> None:
        pass

    # Implementar solo lo que las capabilities declaran:
    def predict_image(self, frame: np.ndarray) -> list[Detection]:
        raise NotImplementedError(f"{self.info.name} no soporta IMAGE")

    def create_session(self, config: dict[str, Any]) -> StreamSession:
        raise NotImplementedError(f"{self.info.name} no soporta STREAM")

    def predict_clip(self, frames: Iterable[tuple[np.ndarray, float]]) -> list[Event]:
        # Default: un clip es un stream acotado — gratis para modelos STREAM.
        session = self.create_session({})
        try:
            return [e for frame, ts in frames for e in session.process_frame(frame, ts)]
        finally:
            session.close()
