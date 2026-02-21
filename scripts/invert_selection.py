"""
Script to invert the current selection in the image viewer.
If some images are selected, it will select all other images instead.
If no images are selected, it will select all images.
"""

def run_script(api):
    # Get all available images
    all_images = set(api.get_all_images())
    
    # Get currently selected images
    selected_images = api.get_selected_images()
    
    # Calculate images to select (all images except currently selected ones)
    images_to_select = all_images - selected_images
    
    # If nothing was selected, this will select everything
    # If something was selected, this will select everything else
    api.set_selected_images(list(images_to_select), clear_existing=True)
