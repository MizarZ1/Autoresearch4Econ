"""获取并校验本地 AJR 输入；作者文件仅保留本机。"""

import hashlib
import io
import shutil
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
URL = (
    "https://www.dropbox.com/scl/fi/tgs2zy2x6g9cnmberwdno/colonial_origins.zip"
    "?rlkey=kumc7exsfl95vv5ejj1w1agup&dl=1"
)
ARCHIVE_HASH = "c6868b1b5b12eb6f9b4d68ff3442d482306952bacbe621be597120d217edc38c"
DATA_HASH = "2b34cce21b58a3a424a804e97a134106ab7010ef936bb68b3a244d3e271af404"


def verify(payload: bytes, expected: str) -> None:
    if hashlib.sha256(payload).hexdigest() != expected:
        raise ValueError("AJR 校验值发生变化，请先核对来源再更新校验基准")


def main() -> int:
    archive_path = ROOT / "local_reference/colonial_origins.zip"
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    if archive_path.exists():
        payload = archive_path.read_bytes()
    else:
        with urllib.request.urlopen(URL, timeout=60) as response:
            payload = response.read()
        verify(payload, ARCHIVE_HASH)
        archive_path.write_bytes(payload)
    verify(payload, ARCHIVE_HASH)
    original = ROOT / "local_reference/ajr2001/table4"
    original.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        nested_bytes = archive.read("maketable4.zip")
        (original.parent / "readme.TXT").write_bytes(archive.read("readme.TXT"))
    with zipfile.ZipFile(io.BytesIO(nested_bytes)) as table4:
        data = table4.read("maketable4.dta")
        verify(data, DATA_HASH)
        for filename in ("maketable4.dta", "maketable4.do"):
            destination = original / filename
            content = table4.read(filename)
            if destination.exists() and destination.read_bytes() != content:
                raise ValueError(f"已有作者原件与来源不同：{filename}；请保留原件并核对")
            destination.write_bytes(content)
    analysis_copy = ROOT / "examples/demo/data/maketable4.dta"
    analysis_copy.parent.mkdir(parents=True, exist_ok=True)
    if analysis_copy.exists():
        verify(analysis_copy.read_bytes(), DATA_HASH)
    else:
        shutil.copy2(original / "maketable4.dta", analysis_copy)
    print("压缩包和 Table 4 校验通过；作者原件及分析副本仅保留本机。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
