from agent.context_compressor import ContextCompressor


def test_raw_budget_fallback_skips_small_post_compaction_rough_overage():
    compressor = ContextCompressor(
        model="test/model",
        config_context_length=128_000,
        threshold_percent=0.5,
        quiet_mode=True,
    )
    compressor.threshold_tokens = 64_000
    compressor.last_prompt_tokens = -1
    compressor.awaiting_real_usage_after_compression = True
    compressor.last_compression_rough_tokens = 66_000

    assert compressor.should_compress(66_000) is False


def test_raw_budget_fallback_does_not_skip_large_post_compaction_overage():
    compressor = ContextCompressor(
        model="test/model",
        config_context_length=128_000,
        threshold_percent=0.5,
        quiet_mode=True,
    )
    compressor.threshold_tokens = 64_000
    compressor.last_prompt_tokens = -1
    compressor.awaiting_real_usage_after_compression = True
    compressor.last_compression_rough_tokens = 80_000

    assert compressor.should_compress(80_000) is True
