-- ============================================================
-- Reflex Rule Lifecycle SQL Functions
-- DevForge — Pattern 2+4 auto-fix system
-- Single-DB approach: all functions operate on observations + reflex_rules.
-- ============================================================

-- ============================================================
-- detect_patterns(): Find frequent error/issue patterns from observations
-- Returns candidate rule suggestions based on statistical clustering.
-- ============================================================
CREATE OR REPLACE FUNCTION detect_patterns(
    since_hours INT DEFAULT 24,
    min_occurrences INT DEFAULT 3
)
RETURNS TABLE(
    pattern_category TEXT,
    pattern_source TEXT,
    pattern_observation TEXT,
    occurrence_count BIGINT,
    distinct_sources BIGINT,
    sample_observation TEXT
)
LANGUAGE plpgsql STABLE
AS $$
DECLARE
    rec RECORD;
BEGIN
    FOR rec IN
        SELECT
            o.category,
            o.source,
            LEFT(o.observation, 120) AS obs_prefix,
            COUNT(*) AS cnt,
            COUNT(DISTINCT o.source) AS src_cnt
        FROM observations o
        WHERE o.created_at > NOW() - (since_hours || ' hours')::INTERVAL
          AND o.category IN ('error', 'test_result', 'insight')
        GROUP BY o.category, o.source, LEFT(o.observation, 120)
        HAVING COUNT(*) >= min_occurrences
        ORDER BY COUNT(*) DESC
    LOOP
        pattern_category := rec.category;
        pattern_source := rec.source;
        pattern_observation := rec.obs_prefix;
        occurrence_count := rec.cnt;
        distinct_sources := rec.src_cnt;
        -- Pick most recent full observation as sample
        SELECT o2.observation INTO sample_observation
        FROM observations o2
        WHERE o2.category = rec.category
          AND o2.source = rec.source
          AND LEFT(o2.observation, 120) = rec.obs_prefix
        ORDER BY o2.created_at DESC LIMIT 1;
        RETURN NEXT;
    END LOOP;
END;
$$;

COMMENT ON FUNCTION detect_patterns IS 'Cluster observations by category+source+prefix to find frequent patterns. Returns candidate rule trigger conditions.';


-- ============================================================
-- promote_rules(): Auto-promote candidate rules to approved
-- Rule: same pattern matched ≥3 times in 48h → auto-promote.
-- Only promotes rules with confidence already ≥0.5.
-- ============================================================
CREATE OR REPLACE FUNCTION promote_rules(
    min_confidence REAL DEFAULT 0.5,
    min_observations INT DEFAULT 3,
    window_hours INT DEFAULT 48
)
RETURNS TABLE(rule_id UUID, old_status TEXT, new_status TEXT, confidence REAL)
LANGUAGE plpgsql
AS $$
DECLARE
    r RECORD;
BEGIN
    FOR r IN
        SELECT rr.id, rr.confidence, rr.observation_count
        FROM reflex_rules rr
        WHERE rr.status = 'candidate'
          AND rr.confidence >= min_confidence
          AND (
              rr.observation_count >= min_observations
              OR (
                  SELECT COUNT(*) FROM observations o
                  WHERE o.created_at > NOW() - (window_hours || ' hours')::INTERVAL
                    AND (rr.trigger_category IS NULL OR o.category = rr.trigger_category)
                    AND (rr.trigger_source IS NULL OR o.source = rr.trigger_source)
                    AND (rr.trigger_pattern IS NULL OR o.observation ILIKE '%' || rr.trigger_pattern || '%')
                    AND (rr.trigger_tags = '{}'::jsonb OR o.tags @> rr.trigger_tags)
              ) >= min_observations
          )
    LOOP
        UPDATE reflex_rules
        SET status = 'approved',
            updated_at = NOW()
        WHERE id = r.id;

        rule_id := r.id;
        old_status := 'candidate';
        new_status := 'approved';
        confidence := r.confidence;
        RETURN NEXT;
    END LOOP;
END;
$$;

COMMENT ON FUNCTION promote_rules IS 'Auto-promote candidate rules to approved when pattern meets confidence+count threshold in window.';


-- ============================================================
-- decay_rules(): Demote approved rules to dormant if unused
-- Rule: no match in 30 days → dormant.
-- ============================================================
CREATE OR REPLACE FUNCTION decay_rules(
    dormant_days INT DEFAULT 30
)
RETURNS TABLE(rule_id UUID, old_status TEXT, new_status TEXT, days_since_match INT)
LANGUAGE plpgsql
AS $$
DECLARE
    r RECORD;
BEGIN
    FOR r IN
        SELECT rr.id,
               COALESCE(
                   EXTRACT(DAY FROM NOW() - rr.last_matched_at),
                   EXTRACT(DAY FROM NOW() - rr.created_at)
               )::INT AS days_idle
        FROM reflex_rules rr
        WHERE rr.status IN ('approved', 'candidate')
          AND (
              (rr.last_matched_at IS NOT NULL AND rr.last_matched_at < NOW() - (dormant_days || ' days')::INTERVAL)
              OR
              (rr.last_matched_at IS NULL AND rr.created_at < NOW() - (dormant_days || ' days')::INTERVAL)
          )
    LOOP
        UPDATE reflex_rules
        SET status = 'dormant',
            updated_at = NOW()
        WHERE id = r.id;

        rule_id := r.id;
        old_status := 'approved';
        new_status := 'dormant';
        days_since_match := r.days_idle;
        RETURN NEXT;
    END LOOP;
END;
$$;

COMMENT ON FUNCTION decay_rules IS 'Demote rules with no match in dormant_days to dormant status.';


-- ============================================================
-- match_rule(observation_text, observation_category, observation_tags):
-- Find approved rules matching given observation.
-- Returns rules ordered by confidence DESC.
-- Also updates last_matched_at and observation_count.
-- ============================================================
CREATE OR REPLACE FUNCTION match_rule(
    obs_text TEXT,
    obs_category TEXT DEFAULT NULL,
    obs_tags JSONB DEFAULT '{}'::jsonb,
    obs_source TEXT DEFAULT NULL
)
RETURNS TABLE(
    rule_id UUID,
    description TEXT,
    action_type TEXT,
    action_params JSONB,
    confidence REAL
)
LANGUAGE plpgsql
AS $$
DECLARE
    r RECORD;
BEGIN
    FOR r IN
        SELECT rr.id, rr.description, rr.action_type, rr.action_params, rr.confidence
        FROM reflex_rules rr
        WHERE rr.status = 'approved'
          AND (rr.trigger_category IS NULL OR rr.trigger_category = obs_category OR obs_category IS NULL)
          AND (rr.trigger_source IS NULL OR rr.trigger_source = obs_source OR obs_source IS NULL)
          AND (rr.trigger_pattern IS NULL OR obs_text ILIKE '%' || rr.trigger_pattern || '%')
          AND (rr.trigger_tags = '{}'::jsonb OR obs_tags @> rr.trigger_tags)
        ORDER BY rr.confidence DESC, rr.observation_count DESC
        LIMIT 5
    LOOP
        -- Update match stats
        UPDATE reflex_rules
        SET observation_count = observation_count + 1,
            last_matched_at = NOW(),
            updated_at = NOW()
        WHERE id = r.id;

        rule_id := r.id;
        description := r.description;
        action_type := r.action_type;
        action_params := r.action_params;
        confidence := r.confidence;
        RETURN NEXT;
    END LOOP;
END;
$$;

COMMENT ON FUNCTION match_rule IS 'Find approved rules matching an observation. Updates match stats atomically.';


-- ============================================================
-- daily_rule_report(): Summary of rule activity for daily cron
-- ============================================================
CREATE OR REPLACE FUNCTION daily_rule_report()
RETURNS TABLE(
    section TEXT,
    line TEXT
)
LANGUAGE plpgsql
AS $$
DECLARE
    total_approved INT;
    total_candidate INT;
    total_dormant INT;
    applied_24h INT;
    new_candidates_24h INT;
BEGIN
    SELECT COUNT(*) INTO total_approved FROM reflex_rules WHERE status = 'approved';
    SELECT COUNT(*) INTO total_candidate FROM reflex_rules WHERE status = 'candidate';
    SELECT COUNT(*) INTO total_dormant FROM reflex_rules WHERE status = 'dormant';
    SELECT COUNT(*) INTO applied_24h FROM reflex_rules WHERE last_applied_at > NOW() - INTERVAL '24 hours';
    SELECT COUNT(*) INTO new_candidates_24h FROM reflex_rules WHERE status = 'candidate' AND created_at > NOW() - INTERVAL '24 hours';

    section := 'summary'; line := format('Rules: %s approved, %s candidate, %s dormant', total_approved, total_candidate, total_dormant); RETURN NEXT;
    section := 'summary'; line := format('Applied in 24h: %s | New candidates in 24h: %s', applied_24h, new_candidates_24h); RETURN NEXT;

    section := 'candidates';
    FOR line IN
        SELECT format('[%s] %s (confidence=%.2f, count=%s, created=%s)',
            rr.id::text, COALESCE(rr.description, '(no desc)'),
            rr.confidence, rr.observation_count, rr.created_at::date::text)
        FROM reflex_rules rr WHERE rr.status = 'candidate' ORDER BY rr.confidence DESC
    LOOP
        RETURN NEXT;
    END LOOP;

    section := 'applied';
    FOR line IN
        SELECT format('[%s] %s → %s at %s',
            rr.id::text, COALESCE(rr.description, '(no desc)'),
            rr.action_type, rr.last_applied_at::timestamptz::text)
        FROM reflex_rules rr WHERE rr.last_applied_at > NOW() - INTERVAL '24 hours' ORDER BY rr.last_applied_at DESC
    LOOP
        RETURN NEXT;
    END LOOP;
END;
$$;

COMMENT ON FUNCTION daily_rule_report IS 'Generate daily summary of reflex rule activity for cron reporting.';