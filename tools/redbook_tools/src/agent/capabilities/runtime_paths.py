from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import sys

from .models import CapabilityError


@dataclass(frozen=True)
class RuntimePaths:
    application_root: Path
    tool_package_root: Path
    runtime_root: Path
    python_executable: Path

    @classmethod
    def resolve(cls, runtime_root: Path, *, python_executable: str = '') -> 'RuntimePaths':
        package = Path(__file__).resolve().parents[3]
        application = package.parents[1] if package.parent.name == 'tools' else package
        python = Path(python_executable or os.getenv('REDBOOK_PYTHON_EXECUTABLE') or sys.executable).resolve()
        runtime = Path(runtime_root).resolve()
        for label, path in [('runtime', runtime), ('python', python), ('application', application)]:
            if path.drive.upper() != 'E:':
                raise CapabilityError('RUNTIME_PATH_INVALID', f'{label} 必须位于 E 盘：{path}')
        if not python.is_file():
            raise CapabilityError('MCP_PYTHON_MISSING', f'解释器不存在：{python}', next_action='选择独立程序 .venv 的 Python')
        if not runtime.is_dir() or not (package / 'src').is_dir():
            raise CapabilityError('RUNTIME_PATH_MISSING', '应用、工具包或运行目录不存在')
        return cls(application, package, runtime, python)

    def public(self) -> dict:
        return {key: str(value) for key, value in self.__dict__.items()}
