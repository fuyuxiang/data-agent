from sqlalchemy import text


def test_sample_orders_loaded(sample_conn):
    total = sample_conn.execute(text("SELECT COUNT(*) FROM sample.orders")).scalar()
    assert total == 14


def test_sample_covers_two_months(sample_conn):
    months = sample_conn.execute(
        text(
            "SELECT DISTINCT date_trunc('month', completed_date)::date "
            "FROM sample.orders WHERE completed_date IS NOT NULL ORDER BY 1"
        )
    ).scalars().all()
    assert len(months) == 2


def test_sample_has_cancelled_and_pending_rows(sample_conn):
    # Needed so metric-level status filters are actually exercised.
    statuses = sample_conn.execute(
        text("SELECT DISTINCT status FROM sample.orders ORDER BY 1")
    ).scalars().all()
    assert statuses == ["cancelled", "completed", "pending"]