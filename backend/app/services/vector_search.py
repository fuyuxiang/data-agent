"""
向量检索引擎

功能：
1. CLIP 模型加载
2. 向量编码
3. LanceDB 连接
4. 混合检索（向量+关键词）
"""

from pathlib import Path
from typing import Dict, List, Optional, Tuple

# 全局模型管理器
_model_manager = None
_lancedb_connections = {}


def get_model_manager(config: Dict) -> "ModelManager":
    """
    获取模型管理器单例

    Args:
        config: 配置字典

    Returns:
        ModelManager 实例
    """
    global _model_manager
    if _model_manager is None:
        _model_manager = ModelManager(config)
    return _model_manager


class ModelManager:
    """模型管理器 - 负责加载 CLIP 模型和向量编码"""

    def __init__(self, config: Dict):
        """
        初始化模型管理器

        Args:
            config: 配置字典，包含 model 和 search 配置
        """
        self.config = config
        self._model = None
        self._device = None

    @property
    def model(self):
        """懒加载 CLIP 模型"""
        if self._model is None:
            self._load_model()
        return self._model

    def _load_model(self):
        """加载 CLIP 模型"""
        try:
            from sentence_transformers import SentenceTransformer
            import torch
        except ImportError as e:
            raise RuntimeError("sentence-transformers is required for vector search") from e

        search_cfg = self.config.get("search", {})
        model_name = search_cfg.get("model_name", "openai/clip-vit-base-patch32")

        # 获取缓存目录
        cache_dir = search_cfg.get("cache_dir")
        if cache_dir:
            Path(cache_dir).mkdir(parents=True, exist_ok=True)

        # 检测设备
        self._device = 'cuda' if torch.cuda.is_available() else 'cpu'

        # 加载模型
        if cache_dir:
            self._model = SentenceTransformer(model_name, cache_folder=cache_dir, device=self._device)
        else:
            self._model = SentenceTransformer(model_name, device=self._device)

        print(f"[ModelManager] 模型已加载: {model_name}, 设备: {self._device}")

    def encode_text(self, text: str) -> "np.ndarray":
        """
        编码文本为向量

        Args:
            text: 待编码文本

        Returns:
            向量数组
        """
        import numpy as np
        return self.model.encode(text, convert_to_numpy=True, normalize_embeddings=True)

    def encode_image(self, image_path: Path) -> "np.ndarray":
        """
        编码图像为向量

        Args:
            image_path: 图像文件路径

        Returns:
            向量数组
        """
        from PIL import Image
        import numpy as np

        image = Image.open(image_path).convert("RGB")
        return self.model.encode(image, convert_to_numpy=True, normalize_embeddings=True)

    def rerank(self, query: str, results: List[Dict], top_k: int = 10) -> List[Dict]:
        """
        对检索结果进行重排序

        Args:
            query: 查询文本
            results: 候选结果列表
            top_k: 返回数量

        Returns:
            重排序后的结果
        """
        # 简单实现：直接返回前 top_k 条
        return results[:top_k]


def get_lancedb_connection(lancedb_dir: str) -> "lancedb.DB":
    """
    获取 LanceDB 连接

    Args:
        lancedb_dir: LanceDB 目录路径

    Returns:
        LanceDB 连接对象
    """
    global _lancedb_connections

    if lancedb_dir not in _lancedb_connections:
        try:
            import lancedb
            _lancedb_connections[lancedb_dir] = lancedb.connect(lancedb_dir)
        except ImportError:
            raise RuntimeError("lancedb is required for vector search")

    return _lancedb_connections[lancedb_dir]


def _tokenize(text: str) -> set:
    """中文分词"""
    try:
        import jieba
        return {w for w in jieba.cut_for_search(text) if len(w.strip()) > 0}
    except Exception:
        # fallback: bigram + trigram
        import re
        tokens = set()
        segments = re.findall(r'[\u4e00-\u9fff]+', text)
        for seg in segments:
            for i in range(len(seg) - 1):
                tokens.add(seg[i:i+2])
            for i in range(len(seg) - 2):
                tokens.add(seg[i:i+3])
            if len(seg) >= 2:
                tokens.add(seg)
        for w in re.findall(r'[a-zA-Z0-9]+', text):
            if len(w) > 1:
                tokens.add(w)
        return tokens


def keyword_match_score(query_text: str, summary: str) -> float:
    """
    计算关键词匹配得分

    Args:
        query_text: 查询文本
        summary: 图像理解文本

    Returns:
        匹配得分 (0-1)
    """
    if not query_text or not summary:
        return 0.0

    query_text = query_text.lower()
    summary = summary.lower()

    query_words = _tokenize(query_text)
    query_words = {w for w in query_words if len(w) > 1}
    if not query_words:
        return 0.0

    matched_words = sum(1 for word in query_words if word in summary)
    return matched_words / len(query_words)


def hybrid_search(
    table,
    query_vec,
    query_text: Optional[str] = None,
    top_k: int = 10,
    filter_str: Optional[str] = None,
    vector_weight: float = 0.7,
    keyword_weight: float = 0.3,
) -> "pd.DataFrame":
    """
    混合检索：向量相似度 + 关键词匹配

    Args:
        table: LanceDB 表
        query_vec: 查询向量
        query_text: 查询文本（用于关键词匹配）
        top_k: 返回数量
        filter_str: 过滤条件
        vector_weight: 向量相似度权重
        keyword_weight: 关键词匹配权重

    Returns:
        混合检索结果 DataFrame
    """
    import pandas as pd
    import numpy as np

    # 先获取更多候选结果
    candidate_k = min(top_k * 5, 100)

    # 执行向量搜索
    query = table.search(query_vec.tolist()).limit(candidate_k)
    if filter_str:
        query = query.where(filter_str)

    results_df = query.to_pandas()

    # 如果没有查询文本或没有 summary 字段，直接返回向量检索结果
    if not query_text or "summary" not in results_df.columns:
        return results_df.head(top_k)

    # 计算混合得分
    scores = []
    for _, row in results_df.iterrows():
        vector_score = 1.0 / (1.0 + float(row["_distance"]))
        keyword_score = keyword_match_score(query_text, row.get("summary", ""))
        hybrid_score = vector_weight * vector_score + keyword_weight * keyword_score
        scores.append(hybrid_score)

    results_df["hybrid_score"] = scores
    results_df = results_df.sort_values("hybrid_score", ascending=False)

    return results_df.head(top_k)


def vector_search(
    config: Dict,
    query_text: str,
    top_k: int = 10,
    filter_dict: Optional[Dict] = None,
) -> List[Dict]:
    """
    执行向量检索

    Args:
        config: 配置字典
        query_text: 查询文本
        top_k: 返回数量
        filter_dict: 过滤条件字典

    Returns:
        检索结果列表
    """
    search_cfg = config.get("search", {})
    lancedb_dir = search_cfg.get("lancedb_dir", "data/lancedb")

    # 获取模型管理器
    mgr = get_model_manager(config)

    # 编码查询文本
    query_vec = mgr.encode_text(query_text)

    # 连接 LanceDB
    db = get_lancedb_connection(lancedb_dir)

    # 检查表是否存在
    try:
        tables = db.table_names() if hasattr(db, 'table_names') else db.list_tables()
    except Exception:
        tables = []

    if "embeddings" not in tables:
        raise RuntimeError(f"LanceDB 表不存在: embeddings")

    table = db.open_table("embeddings")

    # 构建过滤条件
    lance_filter = None
    if filter_dict:
        # TODO: 实现预过滤逻辑
        pass

    # 获取权重配置
    vector_weight = search_cfg.get("vector_weight", 0.7)
    keyword_weight = search_cfg.get("keyword_weight", 0.3)

    # 执行混合检索
    results_df = hybrid_search(
        table, query_vec,
        query_text=query_text,
        top_k=top_k,
        filter_str=lance_filter,
        vector_weight=vector_weight,
        keyword_weight=keyword_weight,
    )

    # 转换为列表格式
    results = []
    for _, row in results_df.iterrows():
        item = {}
        for col in results_df.columns:
            if col == "vector" or (col.startswith("_") and col != "_distance"):
                continue
            val = row[col]
            if hasattr(val, 'item'):
                val = val.item()
            item[col] = val
        if "hybrid_score" in results_df.columns:
            item["hybrid_score"] = float(row["hybrid_score"])
        results.append(item)

    return results


def semantic_enhance(
    config: Dict,
    sql_results: List[Dict],
    query_text: str,
    top_k: int = 20,
) -> Tuple[Dict, List[Dict]]:
    """
    语义增强：对 SQL 查询结果进行语义相似度计算

    Args:
        config: 配置字典
        sql_results: SQL 查询结果列表
        query_text: 查询文本
        top_k: 向量检索候选数量

    Returns:
        (semantic_scores, vector_only_results)
        - semantic_scores: file_path -> hybrid_score 映射
        - vector_only_results: 向量检索独有结果
    """
    if not sql_results:
        return {}, []

    # 获取有 file_path 的结果
    sql_with_files = [r for r in sql_results if r.get("file_path")]
    if not sql_with_files:
        return {}, []

    # 执行向量检索
    search_results = vector_search(config, query_text, top_k=top_k)

    # 构建 file_path -> score 映射
    semantic_scores = {}
    vector_results_map = {}

    for row in search_results:
        fp = row.get("file_path", "")
        if not fp:
            continue
        from pathlib import Path
        fname = Path(fp).name
        score = row.get("hybrid_score", row.get("_distance", 0))
        semantic_scores[fname] = score
        vector_results_map[fname] = row

    # 找出 SQL 结果中没有的向量检索结果
    sql_fnames = set()
    for r in sql_with_files:
        fp = r.get("file_path", "")
        if fp:
            from pathlib import Path
            sql_fnames.add(Path(fp).name)

    vector_only = []
    for fname, item in vector_results_map.items():
        if fname not in sql_fnames:
            vector_only.append(item)
    vector_only = vector_only[:5]  # 最多推荐 5 条

    return semantic_scores, vector_only
