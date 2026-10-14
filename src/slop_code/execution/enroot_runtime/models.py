"""Environment spec for the enroot backend."""

from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import field_validator

from slop_code.execution.models import EnvironmentSpec


class EnrootConfig(BaseModel):
    """How to launch one enroot image for a session.

    ``image`` is a path to a squashfs produced by ``enroot import`` or
    ``enroot export``, not a ``docker://`` URI. ``enroot create`` cannot
    pull. Environment variables in the path are expanded so a checkout
    can point at ``${SCB_IMAGE_STORE}``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    image: str = Field(description="Path to a .sqsh image.")
    binary: str = Field(default="enroot", description="enroot executable.")
    workdir: str = Field(
        default="/workspace",
        description="Workspace mount point inside the container.",
    )
    mount_workspace: bool = Field(
        default=True,
        description="Bind-mount the session workspace at workdir.",
    )
    extra_mounts: dict[str, str | dict[str, str]] = Field(
        default_factory=dict,
        description="Extra host-to-container mounts. Never a home path.",
    )
    slirp_binary: str = Field(
        default="slirp4netns",
        description="Userspace network helper attached after --net.",
    )
    attach_slirp: bool = Field(
        default=True,
        description="Attach slirp4netns so --net has a default route.",
    )

    @field_validator("image", "binary", "slirp_binary")
    @classmethod
    def _expand(cls, value: str) -> str:
        return os.path.expandvars(value)


class EnrootEnvironmentSpec(EnvironmentSpec):
    """Execution inside an unprivileged enroot container."""

    type: Literal["enroot"] = "enroot"
    enroot: EnrootConfig
