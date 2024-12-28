import yaml
import logging

def load_config(path="config/config.yml"):
    with open(path, "r") as f:
        cfg = yaml.safe_load(f)
    return cfg

def get_logger(name=__name__):
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
    )
    return logging.getLogger(name)
