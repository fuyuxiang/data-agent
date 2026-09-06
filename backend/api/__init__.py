from __future__ import annotations

from flask import Flask


def register_blueprints(app: Flask) -> None:
    from .analysis import bp as analysis_bp
    from .analyses import bp as analyses_bp
    from .automation import bp as automation_bp
    from .catalog import bp as catalog_bp
    from .conversation import bp as conversation_bp
    from .compat_core import bp as compat_core_bp
    from .compat_legacy import bp as compat_legacy_bp
    from .datasource_compat import bp as datasource_compat_bp
    from .delivery import bp as delivery_bp
    from .feishu_bot import bp as feishu_bot_bp
    from .integration import bp as integration_bp
    from .identity import bp as identity_bp
    from .knowledge_compat import bp as knowledge_compat_bp
    from .lifecycle import bp as lifecycle_bp
    from .system_compat import bp as system_compat_bp
    from .workspace import bp as workspace_bp
    from .warehouse import bp as warehouse_bp

    for blueprint in (
        workspace_bp,
        warehouse_bp,
        catalog_bp,
        datasource_compat_bp,
        compat_core_bp,
        compat_legacy_bp,
        analysis_bp,
        analyses_bp,
        conversation_bp,
        delivery_bp,
        feishu_bot_bp,
        automation_bp,
        integration_bp,
        identity_bp,
        knowledge_compat_bp,
        lifecycle_bp,
        system_compat_bp,
    ):
        app.register_blueprint(blueprint)
