-- Addverb ABC Classifier — Asset 1 extract
-- Source: Addverb WES task_details for PetMaxi Noida DC-1
-- Grain: one row per (order_id, pick_date, sku, pick_type)
-- Returns completed picks over a configurable window.

DECLARE @window_days INT = 90;

SELECT
    td.order_id                                              AS order_id,
    CAST(td.created_on AS DATE)                              AS pick_date,
    td.sku                                                   AS sku_code,
    td.type                                                  AS pick_type,
    COUNT(*)                                                 AS n_tasks,
    SUM(COALESCE(NULLIF(td.picked_qty, 0), td.quantity))     AS qty_shipped,
    'pick'                                                   AS line_type,
    'Noida DC-1'                                             AS warehouse_code,
    MIN(td.created_on)                                       AS first_task_ts,
    MAX(td.updated_on)                                       AS last_task_ts,
    MAX(td.zone)                                             AS zone_code,
    MAX(td.uom)                                              AS uom
FROM wes.dbo.task_details td
WHERE td.created_on >= DATEADD(DAY, -@window_days, GETDATE())
  AND td.type   IN ('FULL PICKING','MANUAL PICKING','PARTIAL PICKING')
  AND td.status = 4           -- 4 = completed
  AND td.sku IS NOT NULL
GROUP BY td.order_id, CAST(td.created_on AS DATE), td.sku, td.type
HAVING SUM(COALESCE(NULLIF(td.picked_qty, 0), td.quantity)) > 0
ORDER BY pick_date, order_id, sku;
