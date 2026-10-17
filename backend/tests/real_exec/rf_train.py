"""Real Random Forest training program, the kind a student would actually upload for
kind=rf. Reads CRMS_PART/CRMS_PARTS (set by the real agent, same as production) to pick its
own random seed, trains that many trees on the whole dataset in data.csv, and writes
output/result.json with a JSON (not pickled) representation of each tree - CRMS's combine step
only ever concatenates this, never deserializes an object (see app.pool_routes._job_trees).
"""
import csv
import json
import os

from sklearn.ensemble import RandomForestClassifier

TREES_PER_PART = 3


def tree_to_json(tree):
    """A sklearn DecisionTreeClassifier's internal tree, walked into a plain JSON-serializable
    list of nodes - no pickling, no sklearn-specific object, just arrays any reader can parse."""
    t = tree.tree_
    return {
        "feature": t.feature.tolist(),
        "threshold": t.threshold.tolist(),
        "children_left": t.children_left.tolist(),
        "children_right": t.children_right.tolist(),
        "value": t.value.tolist(),
    }


def main():
    part = int(os.environ.get("CRMS_PART", "1"))
    parts = int(os.environ.get("CRMS_PARTS", "1"))

    with open("data.csv", newline="") as f:
        rows = list(csv.reader(f))
    header, data_rows = rows[0], rows[1:]
    X = [[float(v) for v in row[:-1]] for row in data_rows]
    y = [int(row[-1]) for row in data_rows]

    seed = 1000 * part   # each part's trees are reproducible but distinct from every other part
    forest = RandomForestClassifier(n_estimators=TREES_PER_PART, random_state=seed, max_depth=3)
    forest.fit(X, y)

    trees = [tree_to_json(t) for t in forest.estimators_]

    os.makedirs("output", exist_ok=True)
    with open(os.path.join("output", "result.json"), "w") as f:
        json.dump({"trees": trees}, f)

    print(f"part {part}/{parts}: trained {len(trees)} trees, seed {seed}, "
         f"on {len(X)} rows ({len(header) - 1} features)")


if __name__ == "__main__":
    main()
