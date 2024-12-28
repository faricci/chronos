import yaml
import logging
import os

def load_config(config_path="config/config.yml"):    

    #config_path = os.path.join(os.getcwd(), config_path)

    print(f"Loading configuration from: {config_path}")

    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Could not find config file at {config_path}")

    with open(config_path, "r") as f:
        config = yaml.safe_load(f)
    return config

def get_logger(name=__name__):
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
    )
    return logging.getLogger(name)
