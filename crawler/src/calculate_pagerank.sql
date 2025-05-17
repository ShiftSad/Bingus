CREATE OR REPLACE FUNCTION update_pagerank(
    damping    FLOAT   DEFAULT 0.85,
    max_iter   INT     DEFAULT 20,
    tol        FLOAT   DEFAULT 1e-6
) RETURNS VOID AS $$
DECLARE
    n           INT;
    iter        INT;
    total_diff  FLOAT;
BEGIN
    -- Count total pages
    SELECT COUNT(*) INTO n FROM crawled_urls;
    IF n = 0 THEN
        RAISE NOTICE 'No pages to rank.';
        RETURN;
    END IF;

    -- Temporary tables to hold old and new ranks
    CREATE TEMP TABLE pr_old(url TEXT PRIMARY KEY, rank FLOAT);
    CREATE TEMP TABLE pr_new(url TEXT PRIMARY KEY, rank FLOAT);

    -- Initialize all ranks to 1/n
    INSERT INTO pr_old
    SELECT url, 1.0 / n
      FROM crawled_urls;

    -- Precompute out-degrees
    CREATE TEMP TABLE outdeg AS
    SELECT from_url, COUNT(*) AS deg
      FROM urls
     GROUP BY from_url;

    -- Iteratively update PageRank
    FOR iter IN 1..max_iter LOOP
        -- Build new ranks
        TRUNCATE pr_new;

        INSERT INTO pr_new(url, rank)
        SELECT
          cu.url,
          (1 - damping) / n
          + damping * COALESCE(SUM(po.rank::FLOAT / od.deg), 0)
        FROM
          crawled_urls cu
          LEFT JOIN urls u ON cu.url = u.to_url
          LEFT JOIN pr_old po     ON u.from_url = po.url
          LEFT JOIN outdeg od     ON u.from_url = od.from_url
        GROUP BY cu.url;

        -- Compute total change to check convergence
        SELECT SUM(ABS(po.rank - pn.rank)) INTO total_diff
          FROM pr_old po
          JOIN pr_new pn USING (url);

        IF total_diff < tol THEN
            RAISE NOTICE 'Converged after % iterations (Δ=%)', iter, total_diff;
            EXIT;
        END IF;

        -- Swap pr_old ← pr_new
        UPDATE pr_old o
           SET rank = n.rank
          FROM pr_new n
         WHERE o.url = n.url;
    END LOOP;

    -- Write back into crawled_urls.rank
    UPDATE crawled_urls c
       SET rank = p.rank
      FROM pr_old p
     WHERE c.url = p.url;

    -- Clean up
    DROP TABLE pr_old;
    DROP TABLE pr_new;
    DROP TABLE outdeg;

    RAISE NOTICE 'PageRank updated over % pages.', n;
END;
$$ LANGUAGE plpgsql;
