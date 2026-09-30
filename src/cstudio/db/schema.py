"""Versioned SQLite schema.

Migrations are applied in order and tracked with ``PRAGMA user_version``.
Never edit a released migration - append a new one.
"""

from __future__ import annotations

MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE schema_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE installation (
            id INTEGER PRIMARY KEY,
            root_path TEXT NOT NULL UNIQUE,
            label TEXT,
            first_seen TEXT NOT NULL,
            last_scanned TEXT
        );

        CREATE TABLE scan (
            id INTEGER PRIMARY KEY,
            installation_id INTEGER NOT NULL REFERENCES installation(id) ON DELETE CASCADE,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            mode TEXT NOT NULL,
            parser_version INTEGER NOT NULL,
            stats_json TEXT,
            error TEXT
        );

        -- physical files of the installation (archives, indexes, loose files)
        CREATE TABLE source_file (
            id INTEGER PRIMARY KEY,
            installation_id INTEGER NOT NULL REFERENCES installation(id) ON DELETE CASCADE,
            rel_path TEXT NOT NULL,
            kind TEXT NOT NULL,
            size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            last_seen_scan INTEGER,
            UNIQUE (installation_id, rel_path)
        );

        -- every entry listed by every PAMT index
        CREATE TABLE archive_entry (
            id INTEGER PRIMARY KEY,
            installation_id INTEGER NOT NULL REFERENCES installation(id) ON DELETE CASCADE,
            package TEXT NOT NULL,
            vpath TEXT NOT NULL,
            ext TEXT NOT NULL,
            paz_index INTEGER NOT NULL,
            offset INTEGER NOT NULL,
            comp_size INTEGER NOT NULL,
            orig_size INTEGER NOT NULL,
            flags INTEGER NOT NULL,
            UNIQUE (installation_id, package, vpath)
        );
        CREATE INDEX idx_archive_entry_ext ON archive_entry(installation_id, ext);
        CREATE INDEX idx_archive_entry_vpath ON archive_entry(vpath);

        -- analyzed audio-relevant files (archive entries, loose files, bank-embedded media)
        CREATE TABLE asset (
            id INTEGER PRIMARY KEY,
            installation_id INTEGER NOT NULL REFERENCES installation(id) ON DELETE CASCADE,
            origin TEXT NOT NULL,
            locator TEXT NOT NULL,
            vpath TEXT NOT NULL,
            ext TEXT NOT NULL,
            size INTEGER NOT NULL,
            archive_entry_id INTEGER,
            parent_asset_id INTEGER REFERENCES asset(id) ON DELETE CASCADE,
            fingerprint TEXT NOT NULL,
            content_hash TEXT,
            hash_kind TEXT,
            parser_version INTEGER NOT NULL,
            analyzed_scan_id INTEGER,
            last_seen_scan INTEGER,
            status TEXT NOT NULL,
            error TEXT,
            UNIQUE (installation_id, locator)
        );
        CREATE INDEX idx_asset_ext ON asset(installation_id, ext);

        CREATE TABLE bnk (
            asset_id INTEGER PRIMARY KEY REFERENCES asset(id) ON DELETE CASCADE,
            bank_id INTEGER NOT NULL,
            version INTEGER NOT NULL,
            language_id INTEGER,
            project_id INTEGER,
            bank_type INTEGER,
            object_count INTEGER NOT NULL,
            media_count INTEGER NOT NULL,
            decoded_version INTEGER NOT NULL,
            chunks_json TEXT,
            type_counts_json TEXT,
            errors_json TEXT
        );
        CREATE INDEX idx_bnk_bank_id ON bnk(bank_id);

        CREATE TABLE wem (
            id INTEGER PRIMARY KEY,
            asset_id INTEGER NOT NULL REFERENCES asset(id) ON DELETE CASCADE,
            source_id INTEGER NOT NULL,
            container TEXT NOT NULL,
            bank_asset_id INTEGER REFERENCES asset(id) ON DELETE CASCADE,
            valid INTEGER NOT NULL,
            codec TEXT,
            format_tag INTEGER,
            channels INTEGER,
            sample_rate INTEGER,
            duration_s REAL,
            duration_method TEXT,
            sample_count INTEGER,
            data_size INTEGER,
            loops_json TEXT,
            cues_json TEXT,
            details_json TEXT
        );
        CREATE INDEX idx_wem_source ON wem(source_id);

        CREATE TABLE wwise_object (
            id INTEGER PRIMARY KEY,
            bank_asset_id INTEGER NOT NULL REFERENCES asset(id) ON DELETE CASCADE,
            object_id INTEGER NOT NULL,
            type_code INTEGER NOT NULL,
            type_name TEXT NOT NULL,
            offset INTEGER NOT NULL,
            size INTEGER NOT NULL,
            parse_status TEXT NOT NULL,
            error TEXT,
            fields_json TEXT
        );
        CREATE INDEX idx_wwise_object_id ON wwise_object(object_id);
        CREATE INDEX idx_wwise_object_bank ON wwise_object(bank_asset_id);

        CREATE TABLE object_ref (
            id INTEGER PRIMARY KEY,
            bank_asset_id INTEGER NOT NULL REFERENCES asset(id) ON DELETE CASCADE,
            from_object_id INTEGER NOT NULL,
            to_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            confidence TEXT NOT NULL,
            rel_offset INTEGER
        );
        CREATE INDEX idx_object_ref_from ON object_ref(from_object_id);
        CREATE INDEX idx_object_ref_to ON object_ref(to_id);

        CREATE TABLE media_source (
            id INTEGER PRIMARY KEY,
            bank_asset_id INTEGER NOT NULL REFERENCES asset(id) ON DELETE CASCADE,
            object_id INTEGER NOT NULL,
            owner_type TEXT NOT NULL,
            source_id INTEGER NOT NULL,
            stream_type TEXT,
            in_memory_size INTEGER,
            plugin_id INTEGER,
            details_json TEXT
        );
        CREATE INDEX idx_media_source_sid ON media_source(source_id);

        CREATE TABLE xml_doc (
            asset_id INTEGER PRIMARY KEY REFERENCES asset(id) ON DELETE CASCADE,
            root_tag TEXT,
            schema_version TEXT,
            soundbank_version TEXT,
            recognized INTEGER NOT NULL,
            tag_counts_json TEXT,
            unrecognized_json TEXT,
            errors_json TEXT
        );
        CREATE TABLE xml_bank (
            id INTEGER PRIMARY KEY,
            asset_id INTEGER NOT NULL REFERENCES asset(id) ON DELETE CASCADE,
            bank_id INTEGER NOT NULL, name TEXT, path TEXT, object_path TEXT, language TEXT, attrs_json TEXT
        );
        CREATE TABLE xml_event (
            id INTEGER PRIMARY KEY,
            asset_id INTEGER NOT NULL REFERENCES asset(id) ON DELETE CASCADE,
            event_id INTEGER NOT NULL, name TEXT, object_path TEXT, bank_id INTEGER, attrs_json TEXT
        );
        CREATE INDEX idx_xml_event_id ON xml_event(event_id);
        CREATE TABLE xml_media (
            id INTEGER PRIMARY KEY,
            asset_id INTEGER NOT NULL REFERENCES asset(id) ON DELETE CASCADE,
            media_id INTEGER NOT NULL, short_name TEXT, path TEXT, cache_path TEXT, language TEXT,
            streaming INTEGER, location TEXT, bank_id INTEGER, relation TEXT, prefetch_size INTEGER
        );
        CREATE INDEX idx_xml_media_id ON xml_media(media_id);
        CREATE TABLE xml_object (
            id INTEGER PRIMARY KEY,
            asset_id INTEGER NOT NULL REFERENCES asset(id) ON DELETE CASCADE,
            kind TEXT NOT NULL, object_id INTEGER NOT NULL, name TEXT, parent_id INTEGER, object_path TEXT
        );
        CREATE INDEX idx_xml_object_id ON xml_object(object_id);

        -- resolved names for IDs from every source, with provenance
        CREATE TABLE name (
            id INTEGER PRIMARY KEY,
            id_value INTEGER NOT NULL,
            name TEXT NOT NULL,
            kind TEXT,
            source TEXT NOT NULL,
            hash_verified INTEGER NOT NULL DEFAULT 0,
            UNIQUE (id_value, name, source)
        );
        CREATE INDEX idx_name_id ON name(id_value);

        CREATE TABLE metadata (
            id INTEGER PRIMARY KEY,
            entity_type TEXT NOT NULL,
            entity_key TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT,
            source TEXT NOT NULL
        );
        CREATE INDEX idx_metadata_entity ON metadata(entity_type, entity_key);

        -- derived, recomputed each scan
        CREATE TABLE media_context (
            id INTEGER PRIMARY KEY,
            installation_id INTEGER NOT NULL REFERENCES installation(id) ON DELETE CASCADE,
            source_id INTEGER NOT NULL,
            owner_object_id INTEGER,
            owner_type TEXT,
            container_ids_json TEXT,
            container_types_json TEXT,
            event_ids_json TEXT,
            bank_ids_json TEXT
        );
        CREATE INDEX idx_media_context_sid ON media_context(source_id);

        CREATE TABLE classification (
            id INTEGER PRIMARY KEY,
            installation_id INTEGER NOT NULL REFERENCES installation(id) ON DELETE CASCADE,
            entity_type TEXT NOT NULL,
            entity_key INTEGER NOT NULL,
            role TEXT NOT NULL,
            score REAL NOT NULL,
            confidence TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            scan_id INTEGER
        );
        CREATE INDEX idx_classification_key ON classification(entity_type, entity_key);

        CREATE TABLE unknown_structure (
            id INTEGER PRIMARY KEY,
            installation_id INTEGER NOT NULL REFERENCES installation(id) ON DELETE CASCADE,
            signature TEXT NOT NULL,
            category TEXT NOT NULL,
            entity_type TEXT,
            entity_key TEXT,
            description TEXT NOT NULL,
            details_json TEXT,
            occurrences INTEGER NOT NULL DEFAULT 1,
            first_scan_id INTEGER,
            last_scan_id INTEGER,
            status TEXT NOT NULL DEFAULT 'open',
            knowledge_uid TEXT,
            UNIQUE (installation_id, signature)
        );

        CREATE TABLE research_source (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            kind TEXT NOT NULL,
            url TEXT,
            trust_label TEXT NOT NULL,
            license_note TEXT,
            file_sha1 TEXT,
            imported_at TEXT NOT NULL,
            details_json TEXT
        );
        CREATE TABLE community_media (
            id INTEGER PRIMARY KEY,
            research_source_id INTEGER NOT NULL REFERENCES research_source(id) ON DELETE CASCADE,
            media_id INTEGER NOT NULL,
            description TEXT,
            context TEXT,
            category TEXT,
            original_name TEXT,
            bank_name TEXT
        );
        CREATE INDEX idx_community_media_id ON community_media(media_id);

        CREATE TABLE finding (
            id INTEGER PRIMARY KEY,
            uid TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            statement TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('verified','probable','hypothesis','unknown','rejected')),
            category TEXT,
            subject_type TEXT,
            subject_key TEXT,
            evidence_json TEXT NOT NULL,
            reasoning TEXT,
            verification_json TEXT,
            created_by TEXT NOT NULL,
            model_id TEXT,
            session_id INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX idx_finding_subject ON finding(subject_type, subject_key);

        CREATE TABLE ai_session (
            id INTEGER PRIMARY KEY,
            model_id TEXT,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            status TEXT NOT NULL,
            steps INTEGER NOT NULL DEFAULT 0,
            summary TEXT
        );
        CREATE TABLE ai_step (
            id INTEGER PRIMARY KEY,
            session_id INTEGER NOT NULL REFERENCES ai_session(id) ON DELETE CASCADE,
            step_no INTEGER NOT NULL,
            thought TEXT,
            tool TEXT,
            args_json TEXT,
            result_json TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE model (
            id TEXT PRIMARY KEY,
            display_name TEXT,
            tier TEXT,
            file_path TEXT,
            sha256 TEXT,
            size INTEGER,
            status TEXT NOT NULL,
            verified_at TEXT,
            inference_ok INTEGER,
            details_json TEXT
        );
        """,
    ),
]

LATEST_VERSION = MIGRATIONS[-1][0]
