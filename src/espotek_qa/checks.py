"""Server-side checks (install, environment), recorded in the --auto-qa report schema
so the results site shows them next to the app's own tests."""

from __future__ import annotations

from dataclasses import dataclass, field


class InstallFailed(RuntimeError):
    """An install step failed; --auto-qa cannot run against this target."""


@dataclass
class Checks:
    group: str
    items: list[dict] = field(default_factory=list)

    def record(self, id: str, title: str, ok: bool, message: str = "", artifacts: list[dict] | None = None,
               duration_s: float = 0.0) -> bool:
        self.items.append({
            "id": f"{self.group}.{id}",
            "group": self.group,
            "title": title,
            "status": "pass" if ok else "fail",
            "duration_s": round(duration_s, 2),
            "message": message,
            "measurements": [],
            "artifacts": artifacts or [],
        })
        return ok

    def require(self, id: str, title: str, ok: bool, message: str = "", artifacts: list[dict] | None = None) -> None:
        """Record, and stop the install if it failed."""
        if not self.record(id, title, ok, message, artifacts):
            raise InstallFailed(f"{title}: {message}")

    @property
    def failed(self) -> bool:
        return any(i["status"] != "pass" for i in self.items)
