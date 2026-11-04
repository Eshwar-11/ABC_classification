-- Addverb ABC Classifier — Asset 2 extract
-- Source: wes.dbo.product + derived status from task_details

WITH first_seen AS (
    SELECT sku, MIN(created_on) AS first_pick_at
    FROM wes.dbo.task_details
    WHERE sku IS NOT NULL
    GROUP BY sku
),
last_seen AS (
    SELECT sku, MAX(created_on) AS last_pick_at
    FROM wes.dbo.task_details
    WHERE sku IS NOT NULL
      AND type IN ('FULL PICKING','MANUAL PICKING','PARTIAL PICKING')
      AND status = 4
    GROUP BY sku
)
SELECT
    p.sku_id                                                 AS sku_code,
    p.description                                            AS sku_description,
    p.velocity                                               AS velocity_declared,
    fs.first_pick_at                                         AS launch_date_derived,
    ls.last_pick_at                                          AS last_pick_at,
    CASE
        WHEN fs.first_pick_at IS NULL THEN 'no-history'
        WHEN DATEDIFF(DAY, fs.first_pick_at, GETDATE()) < 60 THEN 'new-sku'
        WHEN ls.last_pick_at IS NULL OR DATEDIFF(DAY, ls.last_pick_at, GETDATE()) > 180 THEN 'dormant'
        ELSE 'active'
    END                                                      AS status_derived,
    'derived-first-pick'                                     AS launch_date_source
FROM wes.dbo.product p
LEFT JOIN first_seen fs ON fs.sku = p.sku_id
LEFT JOIN last_seen  ls ON ls.sku = p.sku_id
ORDER BY p.sku_id;
