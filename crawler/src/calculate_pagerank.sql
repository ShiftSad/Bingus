CREATE OR REPLACE FUNCTION public.update_pagerank(
    damping double precision DEFAULT 0.85,
    max_iter integer DEFAULT 20,
    tol double precision DEFAULT 0.000001
)
 RETURNS void
 LANGUAGE plpgsql
AS $function$
DECLARE
    n_pages         BIGINT; -- Changed to BIGINT for potentially very large number of pages
    n_float         DOUBLE PRECISION;
    iter            INT;
    total_diff      DOUBLE PRECISION;
    start_ts        TIMESTAMPTZ;
    step_ts         TIMESTAMPTZ;
    iter_ts         TIMESTAMPTZ;
    base_rank_part  DOUBLE PRECISION;
    damping_factor  DOUBLE PRECISION;
BEGIN
    start_ts := clock_timestamp();
    RAISE NOTICE '[PROGRESS] Starting PageRank calculation at %', start_ts;

    -- Ensure necessary indexes exist on permanent tables (IMPORTANT!)
    -- CREATE INDEX IF NOT EXISTS idx_crawled_urls_url ON public.crawled_urls(url); -- Assuming url is PK, this is usually implicit
    -- CREATE INDEX IF NOT EXISTS idx_urls_from_url ON public.urls(from_url);
    -- CREATE INDEX IF NOT EXISTS idx_urls_to_url ON public.urls(to_url);
    -- CREATE INDEX IF NOT EXISTS idx_urls_from_to_url ON public.urls(from_url, to_url); -- Composite might be good

    -- Count total pages
    step_ts := clock_timestamp();
    SELECT COUNT(*) INTO n_pages FROM crawled_urls;
    RAISE NOTICE '[PROGRESS] Counted % pages in %', n_pages, clock_timestamp() - step_ts;

    IF n_pages = 0 THEN
        RAISE NOTICE 'No pages to rank.';
        RETURN;
    END IF;
    n_float := n_pages::DOUBLE PRECISION;
    base_rank_part := (1.0 - damping) / n_float;
    damping_factor := damping; -- Just to make query below cleaner

    -- Temporary tables to hold old and new ranks
    -- Using UNLOGGED for performance as these don't need to be crash-safe
    step_ts := clock_timestamp();
    CREATE UNLOGGED TEMP TABLE pr_old(url TEXT PRIMARY KEY, rank DOUBLE PRECISION) ON COMMIT DROP;
    CREATE UNLOGGED TEMP TABLE pr_new(url TEXT PRIMARY KEY, rank DOUBLE PRECISION) ON COMMIT DROP;
    RAISE NOTICE '[PROGRESS] Temp tables pr_old, pr_new created in %', clock_timestamp() - step_ts;

    -- Initialize all ranks to 1/n
    step_ts := clock_timestamp();
    INSERT INTO pr_old (url, rank)
    SELECT cu.url, 1.0 / n_float
      FROM crawled_urls cu;
    ANALYZE pr_old; -- Update stats for the planner
    RAISE NOTICE '[PROGRESS] Initialized pr_old with % ranks in %', n_pages, clock_timestamp() - step_ts;

    -- Precompute out-degrees
    step_ts := clock_timestamp();
    CREATE UNLOGGED TEMP TABLE outdeg (
        from_url TEXT PRIMARY KEY,
        deg INTEGER NOT NULL
    ) ON COMMIT DROP;

    INSERT INTO outdeg (from_url, deg)
    SELECT u.from_url, COUNT(*) AS deg
      FROM urls u
     GROUP BY u.from_url;
    ANALYZE outdeg; -- Update stats for the planner
    RAISE NOTICE '[PROGRESS] Precomputed out-degrees for % urls in %', (SELECT COUNT(*) FROM outdeg), clock_timestamp() - step_ts;

    -- Temp table for incoming rank sum (contribution from other pages)
    CREATE UNLOGGED TEMP TABLE incoming_rank_sum (
        to_url TEXT PRIMARY KEY,
        total_incoming_rank_contrib DOUBLE PRECISION NOT NULL
    ) ON COMMIT DROP;


    -- Iteratively update PageRank
    FOR iter IN 1..max_iter LOOP
        iter_ts := clock_timestamp();
        RAISE NOTICE '[PROGRESS] Iteration %/% starting...', iter, max_iter;

        -- 1. Calculate sum of rank contributions for each target URL
        step_ts := clock_timestamp();
        TRUNCATE incoming_rank_sum;
        INSERT INTO incoming_rank_sum (to_url, total_incoming_rank_contrib)
        SELECT
            u.to_url,
            SUM(po.rank / od.deg) -- od.deg comes from outdeg, which ensures from_url exists and deg > 0
        FROM
            urls u
        JOIN
            pr_old po ON u.from_url = po.url
        JOIN
            outdeg od ON u.from_url = od.from_url -- INNER JOIN is fine, link must have out-degree
        GROUP BY
            u.to_url;
        ANALYZE incoming_rank_sum;
        RAISE NOTICE '[PROGRESS]   Iter %: Incoming contributions calculated in %', iter, clock_timestamp() - step_ts;

        -- 2. Build new ranks using the pre-calculated sums
        step_ts := clock_timestamp();
        TRUNCATE pr_new;
        INSERT INTO pr_new(url, rank)
        SELECT
          cu.url,
          base_rank_part + damping_factor * COALESCE(irs.total_incoming_rank_contrib, 0.0)
        FROM
          crawled_urls cu
        LEFT JOIN
          incoming_rank_sum irs ON cu.url = irs.to_url;
        ANALYZE pr_new;
        RAISE NOTICE '[PROGRESS]   Iter %: New ranks calculated in %', iter, clock_timestamp() - step_ts;

        -- 3. Compute total change to check convergence
        step_ts := clock_timestamp();
        SELECT SUM(ABS(po.rank - pn.rank)) INTO total_diff
          FROM pr_old po
          JOIN pr_new pn USING (url);
        RAISE NOTICE '[PROGRESS]   Iter %: Difference calculated (Δ=%) in %', iter, total_diff, clock_timestamp() - step_ts;

        IF total_diff IS NULL OR total_diff < tol THEN
            RAISE NOTICE '[PROGRESS] Converged after % iterations (Δ=%)', iter, total_diff;
            EXIT; -- Exit FOR loop
        END IF;

        -- 4. Swap pr_old ← pr_new (TRUNCATE + INSERT is often faster than UPDATE for full copy)
        step_ts := clock_timestamp();
        TRUNCATE pr_old;
        INSERT INTO pr_old SELECT url, rank FROM pr_new;
        ANALYZE pr_old; -- Important for next iteration
        RAISE NOTICE '[PROGRESS]   Iter %: Ranks swapped (pr_old updated) in %', iter, clock_timestamp() - step_ts;
        RAISE NOTICE '[PROGRESS] Iteration %/% completed in % (Total diff: %)', iter, max_iter, clock_timestamp() - iter_ts, total_diff;
    END LOOP;

    IF iter > max_iter THEN
        RAISE NOTICE '[PROGRESS] Reached max_iter % without convergence (Δ=%)', max_iter, total_diff;
    END IF;

    -- Write back final ranks into crawled_urls.rank
    RAISE NOTICE '[PROGRESS] Writing final ranks back to crawled_urls...';
    step_ts := clock_timestamp();
    UPDATE crawled_urls c
       SET rank = p.rank
      FROM pr_old p -- pr_old holds the latest converged or max_iter ranks
     WHERE c.url = p.url;
    RAISE NOTICE '[PROGRESS] Final ranks written to crawled_urls in %', clock_timestamp() - step_ts;

    -- Clean up (temp tables with ON COMMIT DROP are dropped automatically at function/transaction end)
    -- DROP TABLE pr_old; -- Not needed due to ON COMMIT DROP
    -- DROP TABLE pr_new; -- Not needed
    -- DROP TABLE outdeg; -- Not needed
    -- DROP TABLE incoming_rank_sum; -- Not needed

    RAISE NOTICE '[PROGRESS] PageRank calculation completed for % pages. Total time: %', n_pages, clock_timestamp() - start_ts;
END;
$function$;
