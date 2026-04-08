#
# Copyright (C) 2022-present ScyllaDB
#
# SPDX-License-Identifier: LicenseRef-ScyllaDB-Source-Available-1.0
#
from typing import Any, Callable, Coroutine, List, Dict, Optional
import asyncio
import logging

Artifact = Coroutine


class ArtifactRegistry:
    """ A global to all tests registry of all external
    resources and artifacts, such as open ports, directories with temporary
    files or running auxiliary processes. Contains a map of all glboal
    resources, and as soon as the resource is taken by the test it is
    represented in the artifact registry. """

    def __init__(self) -> None:
        self.suite_artifacts: Dict[Any, List[Artifact]] = {}
        self.exit_artifacts: Dict[Optional[Any], List[Artifact]] = {}

    async def cleanup_before_exit(self) -> None:
        logging.info("Cleaning up before exit...")
        for artifacts in self.suite_artifacts.values():
            for artifact in artifacts:
                artifact.close()
            await asyncio.gather(*artifacts, return_exceptions=True)
        self.suite_artifacts = {}
        for artifacts in self.exit_artifacts.values():
            await asyncio.gather(*artifacts, return_exceptions=True)
        self.exit_artifacts = {}
        logging.info("Done cleaning up before exit...")

    def add_suite_artifact(self, suite: Any, artifact: Callable[[], Artifact]) -> None:
        self.suite_artifacts.setdefault(suite, []).append(artifact())

    def add_exit_artifact(self, suite: Optional[Any], artifact: Callable[[], Artifact]):
        self.exit_artifacts.setdefault(suite, []).append(artifact())
