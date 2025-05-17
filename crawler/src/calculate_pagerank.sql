CREATE OR REPLACE FUNCTION calculate_pagerank_in_db(
    p_damping_factor FLOAT DEFAULT 0.85,
    p_iterations INT DEFAULT 20
)
RETURNS VOID AS $$
DECLARE
    v_total_urls BIGINT;
    v_base_value FLOAT;
    i INT;
BEGIN
    -- 1. Ensure 'rank' column exists
    RAISE NOTICE 'Checking/Adding rank column...';
    ALTER TABLE crawled_urls ADD COLUMN IF NOT EXISTS rank FLOAT;

    -- 2. Ensure 'outbound_link_count' column exists
    RAISE NOTICE 'Checking/Adding outbound_link_count column...';
    ALTER TABLE crawled_urls ADD COLUMN IF NOT EXISTS outbound_link_count INT;

    -- 3. Populate/Update 'outbound_link_count'
    RAISE NOTICE 'Updating outbound link counts...';
    -- Initialize all to 0 first
    UPDATE crawled_urls SET outbound_link_count = 0;
    -- Then update with actual counts
    WITH link_counts AS (
        SELECT
            from_url,
            COUNT(*) AS o_count
        FROM
            url_links
        GROUP BY
            from_url
    )
    UPDATE crawled_urls c
    SET outbound_link_count = lc.o_count
    FROM link_counts lc
    WHERE c.url = lc.from_url;
    RAISE NOTICE 'Outbound link counts updated.';

    -- 4. Initialize ranks
    RAISE NOTICE 'Initializing ranks to 1.0...';
    UPDATE crawled_urls SET rank = 1.0;
    COMMIT; -- Commit schema changes and initial rank setup

    -- 5. Get total_urls
    SELECT COUNT(*) INTO v_total_urls FROM crawled_urls;
    IF v_total_urls = 0 THEN
        RAISE NOTICE 'No URLs found in crawled_urls. Exiting.';
        RETURN;
    END IF;
    RAISE NOTICE 'Total URLs: %', v_total_urls;

    -- 6. Create a temporary table for new ranks (dropped at end of session or commit)
    CREATE TEMP TABLE IF NOT EXISTS new_pageranks (
        url TEXT PRIMARY KEY,
        new_rank FLOAT
    ) ON COMMIT DROP;

    -- 7. Iterative calculation
    RAISE NOTICE 'Starting PageRank iterations...';
    FOR i IN 1..p_iterations LOOP
        RAISE NOTICE 'Iteration %/%', i, p_iterations;

        -- Clear temp table for current iteration's new ranks
        TRUNCATE TABLE new_pageranks;

        -- Calculate base value for "teleportation"
        v_base_value := (1.0 - p_damping_factor) / v_total_urls;

        -- Calculate new ranks and store them in the temporary table.
        -- This query calculates the sum of rank contributions from incoming links.
        INSERT INTO new_pageranks (url, new_rank)
        SELECT
            target_cu.url,
            v_base_value + p_damping_factor * COALESCE(
                SUM(
                    source_cu.rank / GREATEST(source_cu.outbound_link_count, 1) -- Use GREATEST to prevent division by zero
                ),
                0.0 -- If no incoming links, sum is 0
            ) AS calculated_rank
        FROM
            crawled_urls target_cu -- For every URL in our table...
        LEFT JOIN
            url_links ul ON target_cu.url = ul.to_url -- ...find links pointing TO it
        LEFT JOIN
            crawled_urls source_cu ON ul.from_url = source_cu.url -- ...and get the source of that link
            -- source_cu.rank is the rank from the previous iteration
            -- source_cu.outbound_link_count is the pre-calculated count
        GROUP BY
            target_cu.url;

        -- Update the actual rank column from the temporary table
        UPDATE crawled_urls cu
        SET rank = nr.new_rank
        FROM new_pageranks nr
        WHERE cu.url = nr.url;
        
        -- COMMIT; -- Optional: commit after each iteration if needed for very long processes,
                  -- but generally better to commit at the end for atomicity.
    END LOOP;

    RAISE NOTICE 'PageRank calculation completed after % iterations.', p_iterations;

EXCEPTION
    WHEN OTHERS THEN
        RAISE NOTICE 'Error calculating PageRank: %', SQLERRM;
        RAISE; -- Re-raise the error to ensure transaction rollback
END;
$$ LANGUAGE plpgsql;