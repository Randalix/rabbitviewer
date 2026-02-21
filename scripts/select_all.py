import logging

def run_script(api):
    """
    Selects all images in the current view.
    """
    logging.debug("Running select_all script.")
    try:
        all_images = api.get_all_images()
        if all_images:
            api.set_selected_images(all_images, clear_existing=True)
            logging.info(f"Script selected {len(all_images)} images.")
        else:
            logging.info("Script select_all: No images to select.")
    except Exception as e:
        logging.error(f"Error in select_all script: {e}", exc_info=True)
