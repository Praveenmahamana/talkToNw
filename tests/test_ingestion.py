"""
Unit tests for schedule ingestion — loader, normaliser, and SSIM parsing.
"""

import pytest
import pandas as pd
import tempfile
import csv
from pathlib import Path
from datetime import datetime


class TestLoader:

    def test_load_csv_folder(self, sample_csv_folder):
        from app.ingestion.loader import load_schedule_folder
        df, report = load_schedule_folder(sample_csv_folder)
        assert not df.empty
        assert report["total_rows"] > 0

    def test_invalid_folder_raises(self):
        from app.ingestion.loader import load_schedule_folder
        with pytest.raises(ValueError, match="Not a directory"):
            load_schedule_folder("/nonexistent/path/xyz")

    def test_empty_folder_returns_empty(self):
        from app.ingestion.loader import load_schedule_folder
        with tempfile.TemporaryDirectory() as tmpdir:
            df, report = load_schedule_folder(tmpdir)
            assert df.empty

    def test_ssim_file_parsing(self):
        from app.ingestion.loader import load_ssim_file
        # Create a minimal SSIM Type-3 record (padded to 200 chars)
        # Format: 3[suf][al ][flt ][iv][ls][svc][period_from][period_to][days   ][fr][dep][depP][depA][utcD ][arr][arrP][arrA][utcA ][ac ]...
        ssim_line = (
            "3 EK 0001 1 101JAN2431MAR241234567 DXB08000800+0400LHR1300130000:00B77"
        )
        ssim_line = ssim_line.ljust(200)

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".ssim", delete=False, encoding="utf-8"
        ) as f:
            f.write(ssim_line + "\n")
            tmppath = Path(f.name)

        try:
            df, warnings = load_ssim_file(tmppath)
            # Should parse at least 1 record or raise no unhandled exceptions
            assert isinstance(df, pd.DataFrame)
        finally:
            tmppath.unlink(missing_ok=True)


class TestNormaliser:

    def test_normalise_clean_csv(self, sample_csv_folder):
        from app.ingestion.loader import load_schedule_folder
        from app.ingestion.normalizer import normalise

        raw_df, _ = load_schedule_folder(sample_csv_folder)
        norm_df, skipped = normalise(raw_df)

        assert not norm_df.empty
        # Check canonical columns exist
        for col in ["origin", "destination", "departure_local", "id"]:
            assert col in norm_df.columns

    def test_normalise_missing_required_columns(self):
        from app.ingestion.normalizer import normalise
        df = pd.DataFrame({"random_col": ["a", "b"]})
        result, skipped = normalise(df)
        assert result.empty
        assert len(skipped) > 0

    def test_normalise_alias_columns(self):
        from app.ingestion.normalizer import normalise
        df = pd.DataFrame({
            "ORG":     ["DXB", "DXB"],
            "DST":     ["LHR", "BOM"],
            "STD":     ["08:00", "09:30"],
            "STA":     ["13:00", "14:00"],
            "AL":      ["EK", "EK"],
            "FLT":     ["EK500", "EK600"],
            "EQUIP":   ["B777", "B738"],
            "FREQ":    ["1234567", "135"],
        })
        result, skipped = normalise(df)
        assert not result.empty
        assert "origin" in result.columns
        assert "destination" in result.columns

    def test_frequency_expansion(self):
        from app.ingestion.normalizer import normalise
        df = pd.DataFrame({
            "origin": ["DXB"], "destination": ["LHR"],
            "departure_local": ["08:00"], "arrival_local": ["13:00"],
            "airline": ["EK"], "flight_number": ["EK500"],
            "frequency": ["135"],  # Mon, Wed, Fri only → 3 rows
        })
        result, _ = normalise(df)
        assert len(result) == 3
        days = sorted(result["day_of_operation"].tolist())
        assert days == [1, 3, 5]
