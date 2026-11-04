-- Addverb ABC Classifier — Asset 3 extract
-- Source: fg.dbo container + location trackers joined to product_material

WITH mrt AS (
    SELECT container, tracker
    FROM (
        SELECT ROW_NUMBER() OVER (PARTITION BY container ORDER BY created_time DESC) AS rn,
               container, tracker
        FROM fg.dbo.container_tracker
    ) a WHERE rn = 1
),
mrl AS (
    SELECT tracker, location
    FROM (
        SELECT ROW_NUMBER() OVER (PARTITION BY tracker ORDER BY created_time DESC) AS rn,
               tracker, location
        FROM fg.dbo.location_tracker
    ) a WHERE rn = 1
),
occupied AS (
    SELECT mrt.container, mrl.location AS bin_id, pm.sku, pm.quantity
    FROM mrt
    JOIN mrl                        ON mrl.tracker  = mrt.tracker
    JOIN fg.dbo.product_material pm ON pm.container = mrt.container
    WHERE pm.status = 0
)
SELECT
    o.sku                                                    AS sku_code,
    'Noida DC-1'                                             AS warehouse_code,
    SUM(o.quantity)                                          AS qty_on_hand,
    COUNT(DISTINCT o.container)                              AS n_containers,
    COUNT(DISTINCT o.bin_id)                                 AS n_bins_occupied,
    CAST(SYSUTCDATETIME() AS DATE)                           AS snapshot_date
FROM occupied o
WHERE o.sku IS NOT NULL
GROUP BY o.sku
ORDER BY qty_on_hand DESC;
