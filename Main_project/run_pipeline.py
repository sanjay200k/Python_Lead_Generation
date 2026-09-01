import pandas as pd

CSV_PATH = "lead scaping data/lead_search_criteria (2).csv"

# Which row to pick (1 = first row, 2 = second row, etc.)
ROW_NUMBER =2

df = pd.read_csv(CSV_PATH)
df = df.dropna(how="all")          # drop blank template row
df = df.reset_index(drop=True)     # renumber rows cleanly after dropping

total_rows = len(df)

if ROW_NUMBER < 1 or ROW_NUMBER > total_rows:
    print(f"Out of range! CSV has {total_rows} rows, but you asked for row {ROW_NUMBER}.")
else:
    row = df.iloc[ROW_NUMBER - 1]   # -1 because ROW_NUMBER is 1-indexed, pandas is 0-indexed
    print(f"--- Row {ROW_NUMBER} ---")
    for col in df.columns:
        print(f"{col}: {row[col]}")