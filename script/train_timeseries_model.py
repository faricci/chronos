# train_timeseries_model.py

import os
import pandas as pd
from datetime import datetime
import torch

from datasets import Dataset
from transformers import (
    AutoConfig,
    InformerConfig,
    InformerForPrediction,
    Trainer,
    TrainingArguments,
)

# Example: We'll assume you're using "Informer" from HF
# but the process is similar for Autoformer or TimeSeriesTransformer

MODEL_CKPT = "myorg/informer-crypto"  # e.g. a Hugging Face repo name
LOCAL_DIR = "./model_checkpoints"

def load_historical_data(csv_path):
    """
    Load your full historical dataset (e.g. merges older + new).
    """
    df = pd.read_csv(csv_path)
    # Ensure correct date/time and sorting
    df["time"] = pd.to_datetime(df["time"])
    df.sort_values("time", inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df

def prepare_dataset(df):
    """
    Convert the DataFrame to a Hugging Face Dataset for time-series.
    Typically, you'd include 'past_values', 'future_values', etc.
    We'll show a simplified version.
    """
    # For multi-step forecasting, label could be the next N values of 'close', etc.
    # Here we do a toy approach: next 1 step as label
    # In a real scenario, you'd create sequences with sliding windows.
    data_dict = {
        "time": df["time"].astype(str).tolist(),
        "open": df["open"].tolist(),
        "high": df["high"].tolist(),
        "low":  df["low"].tolist(),
        "close": df["close"].tolist(),
        "volume": df["volume"].tolist()
    }
    ds = Dataset.from_dict(data_dict)
    return ds

def train_informer_model(csv_path, output_dir=LOCAL_DIR, push_to_hub=False):
    df = load_historical_data(csv_path)
    dataset = prepare_dataset(df)

    # Example config: create or load from hugging face
    try:
        # If model exists, load
        model = InformerForPrediction.from_pretrained(MODEL_CKPT)
        print("Loaded existing model from Hugging Face Hub.")
    except:
        # Otherwise create a new config
        config = InformerConfig(
            prediction_length=1,  # forecast horizon
            context_length=64,    # input sequence length
            d_model=32,
            # ...
        )
        model = InformerForPrediction(config)

    # Convert HF dataset to a format suitable for training
    # Usually, you'd define a custom collator or transform that produces
    # (past_values, future_values) pairs. We'll skip details here.

    # Create a small dummy train/test split for demonstration
    ds_train = dataset.select(range(int(0.8 * len(dataset))))
    ds_eval = dataset.select(range(int(0.8 * len(dataset)), len(dataset)))

    # Training arguments
    training_args = TrainingArguments(
        output_dir=output_dir,
        evaluation_strategy="steps",
        eval_steps=100,
        save_steps=200,
        num_train_epochs=1,
        per_device_train_batch_size=16,
        per_device_eval_batch_size=16,
        push_to_hub=push_to_hub,
        hub_model_id=MODEL_CKPT if push_to_hub else None,
        hub_strategy="every_save" if push_to_hub else "end",
    )

    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=ds_train,
        eval_dataset=ds_eval,
    )

    trainer.train()
    trainer.save_model(output_dir)
    print(f"Model saved to {output_dir}")

    if push_to_hub:
        trainer.push_to_hub()
        print(f"Model pushed to {MODEL_CKPT}")

if __name__ == "__main__":
    csv_path = "./data/historical_all_products.csv"
    # Periodically run this script (e.g., weekly) to incorporate new data
    train_informer_model(csv_path, output_dir=LOCAL_DIR, push_to_hub=False)
