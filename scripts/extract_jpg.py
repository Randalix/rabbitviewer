import logging
from pathlib import Path

logger = logging.getLogger(__name__)
from plugins.exiftool_process import ExifToolProcess, is_exiftool_available
from scripts.script_api import ScriptAPI


RAW_EXTENSIONS = {".cr3", ".cr2", ".nef", ".arw", ".orf", ".rw2", ".dng", ".raf", ".pef", ".srw"}


def run_script(api: ScriptAPI, selected_images: list[str] | None = None):
    """Extract the embedded high-res JPEG from selected RAW files.

    Uses exiftool to extract JpgFromRaw (falling back to PreviewImage)
    and writes it next to the original file with a .jpg extension.
    """
    if selected_images is None:
        selected_images = list(api.get_selected_images())

    if not selected_images:
        logger.info("No images selected for JPEG extraction.")
        return

    if not is_exiftool_available():
        logger.error("exiftool not found. Cannot extract embedded JPEGs.")
        return

    raw_files = [p for p in selected_images if Path(p).suffix.lower() in RAW_EXTENSIONS]
    if not raw_files:
        logger.info("No RAW files in selection.")
        return

    et = ExifToolProcess()
    extracted = []
    skipped = []
    failed = []

    try:
        for raw_path in raw_files:
            raw = Path(raw_path)
            out_path = raw.with_suffix(".jpg")

            if out_path.exists():
                logger.info(f"Skipping {raw.name}: {out_path.name} already exists.")
                skipped.append(raw_path)
                continue

            # Try JpgFromRaw first (highest quality), fall back to PreviewImage
            for tag in ("-JpgFromRaw", "-PreviewImage"):
                try:
                    data = et.execute([tag, "-b", str(raw)])
                    if data:
                        out_path.write_bytes(data)
                        logger.info(f"Extracted {tag[1:]} from {raw.name} -> {out_path.name}")
                        extracted.append(str(out_path))
                        break
                except (RuntimeError, TimeoutError) as e:
                    logger.warning(f"Failed {tag} for {raw.name}: {e}")
            else:
                logger.warning(f"No embedded JPEG found in {raw.name}.")
                failed.append(raw_path)
    finally:
        et.terminate()

    # Add extracted JPEGs to the view
    if extracted:
        api.add_images(extracted)

    summary = f"Extracted {len(extracted)}, skipped {len(skipped)}, failed {len(failed)}"
    logger.info(summary)
    api.show_overlay(raw_files, "text", {"text": summary}, duration=2000)
