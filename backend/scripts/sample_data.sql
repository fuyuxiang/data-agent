DROP TABLE IF EXISTS sample.orders;

CREATE TABLE sample.orders (
    order_id        SERIAL PRIMARY KEY,
    order_no        VARCHAR(32)  NOT NULL,
    customer_id     INTEGER      NOT NULL,
    customer_name   VARCHAR(64)  NOT NULL,
    region_code     VARCHAR(8)   NOT NULL,
    province        VARCHAR(32)  NOT NULL,
    channel         VARCHAR(16)  NOT NULL,
    amount          NUMERIC(14,2) NOT NULL,
    cost            NUMERIC(14,2) NOT NULL,
    quantity        INTEGER      NOT NULL,
    is_new_customer BOOLEAN      NOT NULL,
    status          VARCHAR(16)  NOT NULL,
    created_date    DATE         NOT NULL,
    completed_date  DATE
);

-- July 2026 (comparison baseline) and August 2026 (current period).
-- EC = 华东, SC = 华南, NC = 华北.
INSERT INTO sample.orders
    (order_no, customer_id, customer_name, region_code, province, channel,
     amount, cost, quantity, is_new_customer, status, created_date, completed_date)
VALUES
    ('SO202607001', 1001, '江苏机械', 'EC', '江苏', 'online',  120000.00,  78000.00, 12, false, 'completed', '2026-07-03', '2026-07-05'),
    ('SO202607002', 1002, '浙江电子', 'EC', '浙江', 'offline',  95000.00,  62000.00,  8, false, 'completed', '2026-07-08', '2026-07-10'),
    ('SO202607003', 1003, '上海贸易', 'EC', '上海', 'online',   61000.00,  40000.00,  5, true,  'completed', '2026-07-15', '2026-07-16'),
    ('SO202607004', 2001, '广东制造', 'SC', '广东', 'online',   88000.00,  59000.00,  9, false, 'completed', '2026-07-20', '2026-07-22'),
    ('SO202607005', 3001, '北京科技', 'NC', '北京', 'offline',  54000.00,  35000.00,  4, true,  'completed', '2026-07-25', '2026-07-27'),
    ('SO202607006', 1001, '江苏机械', 'EC', '江苏', 'online',   30000.00,  20000.00,  3, false, 'cancelled', '2026-07-28', NULL),
    ('SO202608001', 1001, '江苏机械', 'EC', '江苏', 'online',  142000.00,  91000.00, 14, false, 'completed', '2026-08-02', '2026-08-04'),
    ('SO202608002', 1002, '浙江电子', 'EC', '浙江', 'offline', 110000.00,  71000.00, 10, false, 'completed', '2026-08-05', '2026-08-07'),
    ('SO202608003', 1004, '江苏精密', 'EC', '江苏', 'online',   47000.00,  31000.00,  4, true,  'completed', '2026-08-06', '2026-08-08'),
    ('SO202608004', 1003, '上海贸易', 'EC', '上海', 'online',   66000.00,  43000.00,  6, false, 'completed', '2026-08-09', '2026-08-10'),
    ('SO202608005', 2001, '广东制造', 'SC', '广东', 'online',   97000.00,  64000.00, 10, false, 'completed', '2026-08-03', '2026-08-05'),
    ('SO202608006', 2002, '深圳电器', 'SC', '广东', 'offline',  52000.00,  34000.00,  5, true,  'completed', '2026-08-11', '2026-08-12'),
    ('SO202608007', 3001, '北京科技', 'NC', '北京', 'offline',  58000.00,  38000.00,  5, false, 'completed', '2026-08-07', '2026-08-09'),
    ('SO202608008', 1002, '浙江电子', 'EC', '浙江', 'online',   41000.00,  27000.00,  4, false, 'pending',   '2026-08-12', NULL);