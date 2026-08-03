"""报告归档工具。"""

import zipfile
from pathlib import Path
from typing import BinaryIO


def write_report_zip(report_dir: Path, dirname: str, output: BinaryIO) -> None:
    """把报告目录写入 ZIP；符号链接不会被跟随或打包。"""
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(report_dir.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(report_dir)
            archive.write(path, arcname=str(Path(dirname) / relative))
