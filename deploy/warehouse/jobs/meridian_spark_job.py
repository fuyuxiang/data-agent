"""Reviewed Livy batch entrypoint. It accepts a bounded JobSpec, never source code."""
from __future__ import annotations

import argparse
import json
import re
from typing import Any

from pyspark.ml.classification import LogisticRegression
from pyspark.ml.clustering import KMeans
from pyspark.ml.evaluation import BinaryClassificationEvaluator, ClusteringEvaluator
from pyspark.ml.feature import VectorAssembler
from pyspark.sql import DataFrame, SparkSession, Window, functions as F


IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


def column(name: Any) -> str:
    value = str(name or "")
    if not IDENTIFIER.fullmatch(value):
        raise ValueError(f"invalid column: {value}")
    return value


def load_frame(spark: SparkSession, ref: dict[str, Any]) -> DataFrame:
    uri = str(ref["uri"])
    if uri.startswith("iceberg://"):
        return spark.table("warehouse." + uri.removeprefix("iceberg://"))
    return spark.read.format(str(ref.get("format") or "parquet")).load(uri)


def filter_frame(frame: DataFrame, filters: list[dict[str, Any]]) -> DataFrame:
    operators = {
        "eq": lambda c, v: c == F.lit(v), "ne": lambda c, v: c != F.lit(v),
        "gt": lambda c, v: c > F.lit(v), "gte": lambda c, v: c >= F.lit(v),
        "lt": lambda c, v: c < F.lit(v), "lte": lambda c, v: c <= F.lit(v),
        "in": lambda c, v: c.isin(list(v)),
    }
    for item in filters[:100]:
        op = str(item.get("op") or "eq")
        if op not in operators:
            raise ValueError(f"invalid filter operator: {op}")
        frame = frame.filter(operators[op](F.col(column(item["column"])), item.get("value")))
    return frame


def aggregate(frame: DataFrame, parameters: dict[str, Any]) -> DataFrame:
    frame = filter_frame(frame, list(parameters.get("filters") or []))
    projections = [column(value) for value in parameters.get("columns") or []]
    if projections:
        frame = frame.select(*projections)
    groups = [column(value) for value in parameters.get("group_by") or []]
    functions = {"sum": F.sum, "count": F.count, "avg": F.avg, "min": F.min, "max": F.max}
    expressions = []
    for item in list(parameters.get("aggregations") or [])[:100]:
        function = str(item.get("function") or "")
        if function not in functions:
            raise ValueError(f"invalid aggregate: {function}")
        source = column(item["column"])
        alias = column(item.get("alias") or f"{function}_{source}")
        expressions.append(functions[function](F.col(source)).alias(alias))
    if expressions:
        return frame.groupBy(*groups).agg(*expressions) if groups else frame.agg(*expressions)
    return frame


def window_features(frame: DataFrame, parameters: dict[str, Any]) -> DataFrame:
    partitions = [column(value) for value in parameters.get("partition_by") or []]
    order = column(parameters["order_by"])
    value = column(parameters["value_column"])
    window = Window.partitionBy(*partitions).orderBy(F.col(order)).rowsBetween(
        -max(1, min(int(parameters.get("lookback") or 7), 10000)), 0,
    )
    return frame.withColumn(column(parameters.get("output_column") or "rolling_mean"), F.avg(value).over(window))


def authorized_join(frames: list[DataFrame], parameters: dict[str, Any]) -> DataFrame:
    if len(frames) != 2:
        raise ValueError("authorized_join requires exactly two inputs")
    left_key, right_key = column(parameters["left_key"]), column(parameters.get("right_key") or parameters["left_key"])
    how = str(parameters.get("how") or "inner")
    if how not in {"inner", "left", "right", "full", "left_semi", "left_anti"}:
        raise ValueError("invalid join type")
    return frames[0].join(frames[1], frames[0][left_key] == frames[1][right_key], how)


def grouped_anomaly(frame: DataFrame, parameters: dict[str, Any]) -> DataFrame:
    groups = [column(value) for value in parameters.get("group_by") or []]
    order, value = column(parameters["order_by"]), column(parameters["value_column"])
    window = Window.partitionBy(*groups).orderBy(F.col(order)).rowsBetween(
        -max(2, min(int(parameters.get("lookback") or 28), 10000)), -1,
    )
    mean, std = F.avg(value).over(window), F.stddev_samp(value).over(window)
    return frame.withColumn("trend_mean", mean).withColumn(
        "anomaly_zscore", F.when(std > 0, (F.col(value) - mean) / std).otherwise(F.lit(None)),
    )


def train_model(frame: DataFrame, parameters: dict[str, Any], method: str) -> tuple[DataFrame, dict[str, Any]]:
    features = [column(value) for value in parameters["features"]]
    label = column(parameters.get("label") or "label")
    assembled = VectorAssembler(inputCols=features, outputCol="features").transform(frame).select("features", F.col(label).alias("label"))
    seed = int(parameters.get("seed") or 20260906)
    if method == "mllib_logistic_regression":
        fitted = LogisticRegression(maxIter=min(int(parameters.get("max_iter") or 100), 1000)).fit(assembled)
        predictions = fitted.transform(assembled)
        metric = BinaryClassificationEvaluator().evaluate(predictions)
        return predictions.select("label", "prediction", "probability"), {"area_under_roc": metric}
    fitted = KMeans(k=max(2, min(int(parameters.get("k") or 3), 100)), seed=seed).fit(assembled)
    predictions = fitted.transform(assembled)
    metric = ClusteringEvaluator().evaluate(predictions)
    return predictions.select("prediction", "features"), {"silhouette": metric}


def execute(spark: SparkSession, spec: dict[str, Any]) -> tuple[DataFrame, dict[str, Any]]:
    frames = [load_frame(spark, value) for value in spec["input_refs"]]
    method, parameters = str(spec["method"]), dict(spec.get("parameters") or {})
    if method == "filter_project_aggregate":
        return aggregate(frames[0], parameters), {}
    if method == "window_features":
        return window_features(frames[0], parameters), {}
    if method == "authorized_join":
        return authorized_join(frames, parameters), {}
    if method == "grouped_trend_anomaly":
        return grouped_anomaly(frames[0], parameters), {}
    if method in {"mllib_logistic_regression", "mllib_kmeans"}:
        return train_model(frames[0], parameters, method)
    raise ValueError(f"unsupported method: {method}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-spec-json", required=True)
    spec = json.loads(parser.parse_args().job_spec_json)
    spark = SparkSession.builder.appName("meridian-reviewed-job").getOrCreate()
    output, metrics = execute(spark, spec)
    output_uri = str(spec["output_uri"])
    output.write.mode("errorifexists").parquet(output_uri)
    manifest = {
        "uri": output_uri, "format": "parquet", "schema": output.schema.jsonValue(),
        "schema_ref": f"{output_uri}_schema", "row_count": output.count(),
        "completeness": "complete", "accuracy": "exact", "metrics": metrics,
        "snapshot_set": {str(item["ref_id"]): item.get("snapshot_set") for item in spec["input_refs"]},
        "requested_scope": {"method": spec["method"], "parameters": spec.get("parameters") or {}},
        "actual_scope": {"partitions": output.rdd.getNumPartitions()},
    }
    print("MERIDIAN_RESULT_MANIFEST=" + json.dumps(manifest, ensure_ascii=False, separators=(",", ":")))
    spark.stop()


if __name__ == "__main__":
    main()
