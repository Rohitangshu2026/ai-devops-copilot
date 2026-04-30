import logging

def get_logger():
    logger = logging.getLogger("app_logger")
    logger.setLevel(logging.INFO)

    if logger.hasHandlers():
        return logger

    handler = logging.StreamHandler()
    formatter = logging.Formatter('%(message)s')
    handler.setFormatter(formatter)

    logger.addHandler(handler)

    return logger