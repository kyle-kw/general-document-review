from pathlib import Path
import shutil
import zipfile

SUPPORTED = {".docx": "word/document.xml", ".xlsx": "xl/workbook.xml", ".pptx": "ppt/presentation.xml", ".pdf": None}


def review_directory(data_dir: Path, review_id: str) -> Path:
    if len(review_id) != 32 or any(c not in "0123456789abcdef" for c in review_id):
        raise ValueError("Invalid review identifier")
    return data_dir / "reviews" / review_id


def validate_file(path: Path, max_unpacked_mb: int) -> None:
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED:
        raise ValueError("仅支持 .docx、.xlsx、.pptx、.pdf")
    if suffix == ".pdf":
        with path.open("rb") as stream:
            if b"%PDF-" not in stream.read(1024):
                raise ValueError("文件内容不是有效 PDF")
        return
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if SUPPORTED[suffix] not in names or "[Content_Types].xml" not in names:
                raise ValueError("文件内容与扩展名不匹配")
            if sum(info.file_size for info in archive.infolist()) > max_unpacked_mb * 1024 * 1024:
                raise ValueError("文件解压后超过安全大小限制")
            if len(names) > 100000 or any(info.flag_bits & 1 for info in archive.infolist()):
                raise ValueError("不支持加密或异常压缩文件")
            if any(name.lower().endswith("vbaproject.bin") for name in names):
                raise ValueError("不支持包含宏的文件")
    except zipfile.BadZipFile as exc:
        raise ValueError("文件损坏或已加密，请另存为未加密文件") from exc


def remove_review(data_dir: Path, identifier: str) -> None:
    target = review_directory(data_dir, identifier).resolve()
    root = (data_dir / "reviews").resolve()
    if target.parent != root or target.is_symlink():
        raise ValueError("Unsafe deletion target")
    if target.exists():
        shutil.rmtree(target)
