from __future__ import annotations

from flask import Flask


def register_blueprints(app: Flask) -> None:
    from .admin import bp as admin_bp
    from .analyses import bp as analyses_bp
    from .agents import bp as agents_bp
    from .catalog import bp as catalog_bp
    from .delivery import bp as delivery_bp
    from .integration import bp as integration_bp
    from .identity import bp as identity_bp
    from .knowledge import bp as knowledge_bp
    from .jobs import bp as jobs_bp
    from .library import bp as library_bp
    from .lifecycle import bp as lifecycle_bp
    from .skills import bp as skills_bp
    from .workspace import bp as workspace_bp
    from .warehouse import bp as warehouse_bp

    for blueprint in (
        workspace_bp,
        admin_bp,
        library_bp,
        warehouse_bp,
        catalog_bp,
        analyses_bp,
        agents_bp,
        delivery_bp,
        integration_bp,
        identity_bp,
        knowledge_bp,
        jobs_bp,
        lifecycle_bp,
        skills_bp,
    ):
        app.register_blueprint(blueprint)
