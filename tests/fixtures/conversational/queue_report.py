"""Print the operations queue in input order."""
import csv
from pathlib import Path


def main():
    with Path("queue.csv").open(newline="") as source:
        for item in csv.DictReader(source):
            print(f"{item['id']}: {item['title']} ({item['status']})")


if __name__ == "__main__":
    main()
