import logging
import os
import shutil
import time
from send2trash import send2trash
from scripts.script_api import ScriptAPI # Importiere ScriptAPI

def run_script(api: ScriptAPI):
    """Delete selected images from view, filesystem, and database."""
    start_time = time.time()
    
    selected = api.get_selected_images()
    if not selected:
        logging.info("No images selected for deletion")
        return
        
    selected_list = sorted(list(selected))
    count = len(selected_list)
    
    logging.info(f"Removing {count} images:")
    for path in selected_list:
        logging.info(f"  - {path}")
        
    # 1. Remove from view for immediate UI feedback.
    api.remove_images(selected_list)
    
    # 2. Move files to trash.
    try:
        send2trash(selected_list)
        logging.info(f"Successfully moved {count} files to system trash.")
    except OSError as e:
        if "Directory not found" in str(e):
            logging.warning("System trash on the volume was not found. Attempting to move files to the home trash folder as a fallback.")
            home_trash = os.path.expanduser("~/.Trash")
            try:
                os.makedirs(home_trash, exist_ok=True)
                for path in selected_list:
                    # shutil.move can handle moving files across different drives
                    shutil.move(path, home_trash)
                logging.info(f"Successfully moved {count} files to home trash ({home_trash}).")
            except Exception as fallback_e:
                logging.error(f"Fallback attempt to move files to home trash failed: {fallback_e}", exc_info=True)
                # Abort if the fallback also fails
                return
        else:
            # For other OS errors (e.g., permissions), abort as before.
            logging.error(f"Error moving files to trash: {e}. Database records will not be removed.", exc_info=True)
            return
    except Exception as e:
        # Catch any other unexpected errors
        logging.error(f"An unexpected error occurred while moving files to trash: {e}. Database records will not be removed.", exc_info=True)
        return
    
    # 3. Remove records from database and associated cache files.
    api.remove_image_records(selected_list)
    
    # 4. Get benchmark results
    results = api.get_benchmark_results()
    if results:
        logging.info("\nOperation Performance Metrics:")
        remove_time = results.get('remove_images_time', 0)
        logging.info(f"  Remove from view time: {remove_time:.3f}s")
        if count > 0:
            logging.info(f"  Average time per image: {(remove_time * 1000 / count):.2f}ms")
        else:
            logging.info("  Average time per image: N/A (no images selected)")
        logging.info(f"  Total images remaining: {results.get('total_images', 0)}")
        logging.info(f"  Cached images: {results.get('cached_images', 0)}")
        logging.info(f"  Pending images: {results.get('pending_images', 0)}")
    
    total_time = time.time() - start_time
    logging.info(f"\nTotal script execution time: {total_time:.3f}s")
