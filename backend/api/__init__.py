from __future__ import annotations

from flask import Flask


def register_blueprints(app: Flask) -> None:
    from .analysis import bp as analysis_bp
    from .analyses import bp as analyses_bp
    from .catalog import bp as catalog_bp
    from .conversation import bp as conversation_bp
    from .delivery import bp as delivery_bp
    from .integration import bp as integration_bp
    from .identity import bp as identity_bp
    from .knowledge import bp as knowledge_bp
    from .jobs import bp as jobs_bp
    from .lifecycle import bp as lifecycle_bp
    from .workspace import bp as workspace_bp
    from .warehouse import bp as warehouse_bp

    for blueprint in (
        workspace_bp,
        warehouse_bp,
        catalog_bp,
        analysis_bp,
        analyses_bp,
        conversation_bp,
        delivery_bp,
        integration_bp,
        identity_bp,
        knowledge_bp,
        jobs_bp,
        lifecycle_bp,
    ):
        app.register_blueprint(blueprint)
