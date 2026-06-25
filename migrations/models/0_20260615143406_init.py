from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS "users" (
    "id" UUID NOT NULL  PRIMARY KEY,
    "email" VARCHAR(255) NOT NULL UNIQUE,
    "password" VARCHAR(255),
    "auth_provider" VARCHAR(32) NOT NULL  DEFAULT 'local',
    "provider_id" VARCHAR(255),
    "first_name" VARCHAR(120),
    "last_name" VARCHAR(120),
    "phone_number" VARCHAR(40),
    "profile_image" VARCHAR(512),
    "role" VARCHAR(32) NOT NULL  DEFAULT 'user',
    "status" VARCHAR(32) NOT NULL  DEFAULT 'pending_verification',
    "email_verified" BOOL NOT NULL  DEFAULT False,
    "verification_token" VARCHAR(128),
    "reset_password_token" VARCHAR(128),
    "reset_password_expires" TIMESTAMPTZ,
    "enterprise_id" UUID,
    "last_login_at" TIMESTAMPTZ,
    "last_login_ip" VARCHAR(64),
    "preferences" JSONB,
    "created_at" TIMESTAMPTZ NOT NULL  DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ NOT NULL  DEFAULT CURRENT_TIMESTAMP,
    "deleted_at" TIMESTAMPTZ
);
COMMENT ON COLUMN "users"."auth_provider" IS 'LOCAL: local\nGOOGLE: google\nMICROSOFT: microsoft\nLINKEDIN: linkedin';
COMMENT ON COLUMN "users"."role" IS 'USER: user\nENTERPRISE_USER: enterprise_user\nENTERPRISE_ADMIN: enterprise_admin\nSUPER_ADMIN: super_admin';
COMMENT ON COLUMN "users"."status" IS 'ACTIVE: active\nINACTIVE: inactive\nSUSPENDED: suspended\nPENDING_VERIFICATION: pending_verification';
CREATE TABLE IF NOT EXISTS "audit_logs" (
    "id" UUID NOT NULL  PRIMARY KEY,
    "user_id" UUID,
    "enterprise_id" UUID,
    "action" VARCHAR(64) NOT NULL,
    "severity" VARCHAR(16) NOT NULL  DEFAULT 'info',
    "entity_type" VARCHAR(64),
    "entity_id" VARCHAR(64),
    "description" TEXT,
    "metadata" JSONB,
    "changes" JSONB,
    "ip_address" VARCHAR(64),
    "user_agent" VARCHAR(512),
    "request_id" VARCHAR(128),
    "success" BOOL NOT NULL  DEFAULT True,
    "error_message" TEXT,
    "created_at" TIMESTAMPTZ NOT NULL  DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS "idx_audit_logs_user_id_f7db5c" ON "audit_logs" ("user_id");
CREATE INDEX IF NOT EXISTS "idx_audit_logs_created_bdaee3" ON "audit_logs" ("created_at");
COMMENT ON COLUMN "audit_logs"."action" IS 'USER_LOGIN: user_login\nUSER_LOGOUT: user_logout\nUSER_CREATED: user_created\nUSER_UPDATED: user_updated\nUSER_DELETED: user_deleted\nDATA_CREATED: data_created\nDATA_UPDATED: data_updated\nDATA_DELETED: data_deleted\nDATA_ACCESSED: data_accessed\nDOCUMENT_UPLOADED: document_uploaded\nDOCUMENT_PROCESSED: document_processed\nDOCUMENT_SIGNED: document_signed\nDOCUMENT_SHARED: document_shared\nDOCUMENT_DOWNLOADED: document_downloaded\nDOCUMENT_DELETED: document_deleted\nSIGNATURE_CREATED: signature_created\nSIGNATURE_USED: signature_used\nAPI_KEY_CREATED: api_key_created\nAPI_KEY_USED: api_key_used\nAPI_KEY_REVOKED: api_key_revoked\nSETTINGS_CHANGED: settings_changed\nPERMISSION_GRANTED: permission_granted\nPERMISSION_REVOKED: permission_revoked';
COMMENT ON COLUMN "audit_logs"."severity" IS 'INFO: info\nWARNING: warning\nERROR: error\nCRITICAL: critical';
CREATE TABLE IF NOT EXISTS "data_vault" (
    "id" UUID NOT NULL  PRIMARY KEY,
    "user_id" UUID NOT NULL,
    "segment" VARCHAR(32) NOT NULL,
    "field_name" VARCHAR(64) NOT NULL,
    "encrypted_value" TEXT NOT NULL,
    "source" VARCHAR(32) NOT NULL  DEFAULT 'user_input',
    "source_document_id" UUID,
    "confidence_score" DOUBLE PRECISION,
    "is_active" BOOL NOT NULL  DEFAULT True,
    "is_verified" BOOL NOT NULL  DEFAULT False,
    "verified_at" TIMESTAMPTZ,
    "verified_by" UUID,
    "metadata" JSONB,
    "created_at" TIMESTAMPTZ NOT NULL  DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ NOT NULL  DEFAULT CURRENT_TIMESTAMP,
    "deleted_at" TIMESTAMPTZ,
    CONSTRAINT "uid_data_vault_user_id_c0c84b" UNIQUE ("user_id", "segment", "field_name")
);
CREATE INDEX IF NOT EXISTS "idx_data_vault_user_id_3b9573" ON "data_vault" ("user_id");
CREATE INDEX IF NOT EXISTS "idx_data_vault_segment_a611d9" ON "data_vault" ("segment");
CREATE INDEX IF NOT EXISTS "idx_data_vault_field_n_be44dc" ON "data_vault" ("field_name");
COMMENT ON COLUMN "data_vault"."source" IS 'USER_INPUT: user_input\nDOCUMENT_EXTRACTION: document_extraction\nTHIRD_PARTY: third_party\nAPI_IMPORT: api_import';
CREATE TABLE IF NOT EXISTS "custom_vault_sections" (
    "id" UUID NOT NULL  PRIMARY KEY,
    "user_id" UUID,
    "enterprise_id" UUID,
    "scope" VARCHAR(16) NOT NULL  DEFAULT 'user',
    "name" VARCHAR(120) NOT NULL,
    "key" VARCHAR(64) NOT NULL,
    "icon" VARCHAR(32),
    "display_order" INT NOT NULL  DEFAULT 100,
    "created_at" TIMESTAMPTZ NOT NULL  DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ NOT NULL  DEFAULT CURRENT_TIMESTAMP,
    "deleted_at" TIMESTAMPTZ,
    CONSTRAINT "uid_custom_vaul_user_id_4c6e1e" UNIQUE ("user_id", "key"),
    CONSTRAINT "uid_custom_vaul_enterpr_d8652e" UNIQUE ("enterprise_id", "key")
);
CREATE INDEX IF NOT EXISTS "idx_custom_vaul_user_id_3628a0" ON "custom_vault_sections" ("user_id");
CREATE INDEX IF NOT EXISTS "idx_custom_vaul_enterpr_2d103a" ON "custom_vault_sections" ("enterprise_id");
COMMENT ON COLUMN "custom_vault_sections"."scope" IS 'USER: user\nENTERPRISE: enterprise';
CREATE TABLE IF NOT EXISTS "custom_vault_fields" (
    "id" UUID NOT NULL  PRIMARY KEY,
    "user_id" UUID,
    "enterprise_id" UUID,
    "name" VARCHAR(120) NOT NULL,
    "key" VARCHAR(64) NOT NULL,
    "field_type" VARCHAR(16) NOT NULL  DEFAULT 'text',
    "aliases" JSONB,
    "placeholder" VARCHAR(255),
    "required" BOOL NOT NULL  DEFAULT False,
    "display_order" INT NOT NULL  DEFAULT 100,
    "created_at" TIMESTAMPTZ NOT NULL  DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ NOT NULL  DEFAULT CURRENT_TIMESTAMP,
    "deleted_at" TIMESTAMPTZ,
    "section_id" UUID NOT NULL REFERENCES "custom_vault_sections" ("id") ON DELETE CASCADE,
    CONSTRAINT "uid_custom_vaul_section_6211c1" UNIQUE ("section_id", "key")
);
CREATE INDEX IF NOT EXISTS "idx_custom_vaul_user_id_610921" ON "custom_vault_fields" ("user_id");
CREATE INDEX IF NOT EXISTS "idx_custom_vaul_enterpr_a5ff4b" ON "custom_vault_fields" ("enterprise_id");
COMMENT ON COLUMN "custom_vault_fields"."field_type" IS 'TEXT: text\nNUMBER: number\nFILE: file';
CREATE TABLE IF NOT EXISTS "documents" (
    "id" UUID NOT NULL  PRIMARY KEY,
    "user_id" UUID,
    "enterprise_id" UUID,
    "file_name" VARCHAR(512) NOT NULL,
    "original_file_name" VARCHAR(512) NOT NULL,
    "file_url" VARCHAR(1024) NOT NULL,
    "file_mime_type" VARCHAR(128) NOT NULL,
    "file_size" BIGINT NOT NULL,
    "document_type" VARCHAR(32) NOT NULL  DEFAULT 'other',
    "status" VARCHAR(32) NOT NULL  DEFAULT 'uploaded',
    "processing_mode" VARCHAR(16) NOT NULL  DEFAULT 'auto',
    "template_id" UUID,
    "extracted_fields" JSONB,
    "field_placements" JSONB,
    "overall_confidence_score" DOUBLE PRECISION,
    "is_template" BOOL NOT NULL  DEFAULT False,
    "template_name" VARCHAR(255),
    "version" INT NOT NULL  DEFAULT 1,
    "parent_document_id" UUID,
    "signature_data" JSONB,
    "completed_file_url" VARCHAR(1024),
    "completed_at" TIMESTAMPTZ,
    "routing_mode" VARCHAR(16) NOT NULL  DEFAULT 'parallel',
    "routing_status" VARCHAR(16) NOT NULL  DEFAULT 'draft',
    "sent_at" TIMESTAMPTZ,
    "expires_at" TIMESTAMPTZ,
    "declined_by" UUID,
    "declined_reason" TEXT,
    "shareable_link" VARCHAR(128),
    "shareable_link_expiry" TIMESTAMPTZ,
    "metadata" JSONB,
    "notes" TEXT,
    "created_at" TIMESTAMPTZ NOT NULL  DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ NOT NULL  DEFAULT CURRENT_TIMESTAMP,
    "deleted_at" TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS "idx_documents_user_id_a6d509" ON "documents" ("user_id");
CREATE INDEX IF NOT EXISTS "idx_documents_enterpr_4e4378" ON "documents" ("enterprise_id");
COMMENT ON COLUMN "documents"."document_type" IS 'PASSPORT: passport\nID_CARD: id_card\nDRIVERS_LICENSE: drivers_license\nFORM: form\nCONTRACT: contract\nAPPLICATION: application\nCERTIFICATE: certificate\nOTHER: other';
COMMENT ON COLUMN "documents"."status" IS 'UPLOADED: uploaded\nPROCESSING: processing\nEXTRACTED: extracted\nDRAFT: draft\nPENDING_SIGNATURE: pending_signature\nSIGNED: signed\nCOMPLETED: completed\nFAILED: failed\nARCHIVED: archived';
COMMENT ON COLUMN "documents"."processing_mode" IS 'AUTO: auto\nMANUAL: manual\nHYBRID: hybrid';
COMMENT ON COLUMN "documents"."routing_mode" IS 'PARALLEL: parallel\nSEQUENTIAL: sequential';
COMMENT ON COLUMN "documents"."routing_status" IS 'DRAFT: draft\nSENT: sent\nIN_PROGRESS: in_progress\nCOMPLETED: completed\nDECLINED: declined\nEXPIRED: expired\nVOIDED: voided';
CREATE TABLE IF NOT EXISTS "document_participants" (
    "id" UUID NOT NULL  PRIMARY KEY,
    "document_id" UUID NOT NULL,
    "user_id" UUID,
    "email" VARCHAR(255) NOT NULL,
    "name" VARCHAR(255),
    "role" VARCHAR(16) NOT NULL  DEFAULT 'signer',
    "status" VARCHAR(16) NOT NULL  DEFAULT 'invited',
    "sequence_order" INT NOT NULL  DEFAULT 1,
    "invite_token" VARCHAR(64) NOT NULL UNIQUE,
    "invited_by" UUID NOT NULL,
    "invited_at" TIMESTAMPTZ NOT NULL  DEFAULT CURRENT_TIMESTAMP,
    "first_viewed_at" TIMESTAMPTZ,
    "completed_at" TIMESTAMPTZ,
    "message" TEXT,
    "metadata" JSONB,
    "created_at" TIMESTAMPTZ NOT NULL  DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ NOT NULL  DEFAULT CURRENT_TIMESTAMP,
    "deleted_at" TIMESTAMPTZ,
    CONSTRAINT "uid_document_pa_documen_630a0f" UNIQUE ("document_id", "email")
);
CREATE INDEX IF NOT EXISTS "idx_document_pa_documen_dcafed" ON "document_participants" ("document_id");
CREATE INDEX IF NOT EXISTS "idx_document_pa_user_id_a36998" ON "document_participants" ("user_id");
CREATE INDEX IF NOT EXISTS "idx_document_pa_email_e4a1dc" ON "document_participants" ("email");
CREATE INDEX IF NOT EXISTS "idx_document_pa_invite__647450" ON "document_participants" ("invite_token");
COMMENT ON COLUMN "document_participants"."role" IS 'SIGNER: signer\nREVIEWER: reviewer\nVIEWER: viewer';
COMMENT ON COLUMN "document_participants"."status" IS 'INVITED: invited\nVIEWED: viewed\nSIGNED: signed\nAPPROVED: approved\nDECLINED: declined\nREVOKED: revoked';
CREATE TABLE IF NOT EXISTS "document_comments" (
    "id" UUID NOT NULL  PRIMARY KEY,
    "document_id" UUID NOT NULL,
    "user_id" UUID,
    "participant_id" UUID,
    "author_name" VARCHAR(255) NOT NULL,
    "author_email" VARCHAR(255),
    "body" TEXT NOT NULL,
    "parent_id" UUID,
    "page" INT,
    "x" DOUBLE PRECISION,
    "y" DOUBLE PRECISION,
    "resolved" BOOL NOT NULL  DEFAULT False,
    "resolved_at" TIMESTAMPTZ,
    "resolved_by" UUID,
    "created_at" TIMESTAMPTZ NOT NULL  DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ NOT NULL  DEFAULT CURRENT_TIMESTAMP,
    "deleted_at" TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS "idx_document_co_documen_d8bd9d" ON "document_comments" ("document_id");
CREATE INDEX IF NOT EXISTS "idx_document_co_user_id_97837c" ON "document_comments" ("user_id");
CREATE INDEX IF NOT EXISTS "idx_document_co_partici_7f0d49" ON "document_comments" ("participant_id");
CREATE TABLE IF NOT EXISTS "document_signing_targets" (
    "id" UUID NOT NULL  PRIMARY KEY,
    "document_id" UUID NOT NULL,
    "participant_id" UUID NOT NULL,
    "kind" VARCHAR(16) NOT NULL,
    "page" INT NOT NULL,
    "x" DOUBLE PRECISION NOT NULL,
    "y" DOUBLE PRECISION NOT NULL,
    "width" DOUBLE PRECISION NOT NULL  DEFAULT 180,
    "height" DOUBLE PRECISION NOT NULL  DEFAULT 36,
    "label" VARCHAR(120),
    "filled_at" TIMESTAMPTZ,
    "filled_value" TEXT,
    "sort_order" INT NOT NULL  DEFAULT 100,
    "created_at" TIMESTAMPTZ NOT NULL  DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ NOT NULL  DEFAULT CURRENT_TIMESTAMP,
    "deleted_at" TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS "idx_document_si_documen_965325" ON "document_signing_targets" ("document_id");
CREATE INDEX IF NOT EXISTS "idx_document_si_partici_35b34f" ON "document_signing_targets" ("participant_id");
COMMENT ON COLUMN "document_signing_targets"."kind" IS 'SIGNATURE: signature\nINITIALS: initials\nDATE: date\nTEXT: text';
CREATE TABLE IF NOT EXISTS "webhook_endpoints" (
    "id" UUID NOT NULL  PRIMARY KEY,
    "user_id" UUID,
    "enterprise_id" UUID,
    "url" VARCHAR(512) NOT NULL,
    "events" JSONB,
    "secret" VARCHAR(128) NOT NULL,
    "name" VARCHAR(120),
    "status" VARCHAR(16) NOT NULL  DEFAULT 'active',
    "delivery_attempts" INT NOT NULL  DEFAULT 0,
    "delivery_successes" INT NOT NULL  DEFAULT 0,
    "delivery_failures" INT NOT NULL  DEFAULT 0,
    "consecutive_failures" INT NOT NULL  DEFAULT 0,
    "last_success_at" TIMESTAMPTZ,
    "last_failure_at" TIMESTAMPTZ,
    "last_failure_reason" TEXT,
    "created_at" TIMESTAMPTZ NOT NULL  DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ NOT NULL  DEFAULT CURRENT_TIMESTAMP,
    "deleted_at" TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS "idx_webhook_end_user_id_4860c8" ON "webhook_endpoints" ("user_id");
CREATE INDEX IF NOT EXISTS "idx_webhook_end_enterpr_2a07dd" ON "webhook_endpoints" ("enterprise_id");
COMMENT ON COLUMN "webhook_endpoints"."status" IS 'ACTIVE: active\nPAUSED: paused\nDISABLED: disabled';
CREATE TABLE IF NOT EXISTS "cloud_connections" (
    "id" UUID NOT NULL  PRIMARY KEY,
    "user_id" UUID NOT NULL,
    "provider" VARCHAR(24) NOT NULL,
    "encrypted_access_token" TEXT NOT NULL,
    "encrypted_refresh_token" TEXT,
    "expires_at" TIMESTAMPTZ,
    "account_email" VARCHAR(255),
    "account_name" VARCHAR(255),
    "scopes" JSONB,
    "created_at" TIMESTAMPTZ NOT NULL  DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ NOT NULL  DEFAULT CURRENT_TIMESTAMP,
    "deleted_at" TIMESTAMPTZ,
    CONSTRAINT "uid_cloud_conne_user_id_867ff9" UNIQUE ("user_id", "provider")
);
CREATE INDEX IF NOT EXISTS "idx_cloud_conne_user_id_f0c3b3" ON "cloud_connections" ("user_id");
COMMENT ON COLUMN "cloud_connections"."provider" IS 'GOOGLE_DRIVE: google_drive\nDROPBOX: dropbox\nONEDRIVE: onedrive\nMS365: ms365';
CREATE TABLE IF NOT EXISTS "vault_entries" (
    "id" UUID NOT NULL  PRIMARY KEY,
    "user_id" UUID NOT NULL,
    "section" VARCHAR(32) NOT NULL,
    "encrypted_payload" TEXT NOT NULL,
    "is_current" BOOL NOT NULL  DEFAULT False,
    "sort_order" INT NOT NULL  DEFAULT 100,
    "created_at" TIMESTAMPTZ NOT NULL  DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ NOT NULL  DEFAULT CURRENT_TIMESTAMP,
    "deleted_at" TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS "idx_vault_entri_user_id_141cca" ON "vault_entries" ("user_id");
CREATE INDEX IF NOT EXISTS "idx_vault_entri_section_b8c6fe" ON "vault_entries" ("section");
CREATE TABLE IF NOT EXISTS "signatures" (
    "id" UUID NOT NULL  PRIMARY KEY,
    "user_id" UUID NOT NULL,
    "type" VARCHAR(32) NOT NULL,
    "signature_url" VARCHAR(1024) NOT NULL,
    "signature_name" VARCHAR(255),
    "is_default" BOOL NOT NULL  DEFAULT False,
    "signature_data" TEXT,
    "certificate_id" VARCHAR(255),
    "metadata" JSONB,
    "created_at" TIMESTAMPTZ NOT NULL  DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ NOT NULL  DEFAULT CURRENT_TIMESTAMP,
    "deleted_at" TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS "idx_signatures_user_id_59af52" ON "signatures" ("user_id");
COMMENT ON COLUMN "signatures"."type" IS 'DRAWN: drawn\nTYPED: typed\nUPLOADED: uploaded\nDIGITAL_CERTIFICATE: digital_certificate';
CREATE TABLE IF NOT EXISTS "enterprises" (
    "id" UUID NOT NULL  PRIMARY KEY,
    "name" VARCHAR(255) NOT NULL UNIQUE,
    "domain" VARCHAR(255)  UNIQUE,
    "description" TEXT,
    "logo_url" VARCHAR(1024),
    "status" VARCHAR(32) NOT NULL  DEFAULT 'trial',
    "plan" VARCHAR(32) NOT NULL  DEFAULT 'starter',
    "contact_email" VARCHAR(255),
    "contact_phone" VARCHAR(64),
    "address" JSONB,
    "max_users" INT NOT NULL  DEFAULT 10,
    "max_documents" INT NOT NULL  DEFAULT 1000,
    "max_api_calls" INT NOT NULL  DEFAULT 10000,
    "features" JSONB,
    "custom_branding" JSONB,
    "sso_config" JSONB,
    "webhooks" JSONB,
    "trial_ends_at" TIMESTAMPTZ,
    "subscription_starts_at" TIMESTAMPTZ,
    "subscription_ends_at" TIMESTAMPTZ,
    "metadata" JSONB,
    "created_at" TIMESTAMPTZ NOT NULL  DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ NOT NULL  DEFAULT CURRENT_TIMESTAMP,
    "deleted_at" TIMESTAMPTZ
);
COMMENT ON COLUMN "enterprises"."status" IS 'ACTIVE: active\nINACTIVE: inactive\nSUSPENDED: suspended\nTRIAL: trial';
COMMENT ON COLUMN "enterprises"."plan" IS 'STARTER: starter\nPROFESSIONAL: professional\nENTERPRISE: enterprise\nCUSTOM: custom';
CREATE TABLE IF NOT EXISTS "api_keys" (
    "id" UUID NOT NULL  PRIMARY KEY,
    "name" VARCHAR(255) NOT NULL,
    "key" VARCHAR(128) NOT NULL UNIQUE,
    "description" TEXT,
    "key_type" VARCHAR(8) NOT NULL  DEFAULT 'test',
    "status" VARCHAR(16) NOT NULL  DEFAULT 'active',
    "permissions" JSONB,
    "ip_whitelist" JSONB,
    "usage_count" INT NOT NULL  DEFAULT 0,
    "rate_limit" INT,
    "last_used_at" TIMESTAMPTZ,
    "expires_at" TIMESTAMPTZ,
    "created_at" TIMESTAMPTZ NOT NULL  DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ NOT NULL  DEFAULT CURRENT_TIMESTAMP,
    "deleted_at" TIMESTAMPTZ,
    "enterprise_id" UUID NOT NULL REFERENCES "enterprises" ("id") ON DELETE CASCADE
);
COMMENT ON COLUMN "api_keys"."key_type" IS 'TEST: test\nLIVE: live';
COMMENT ON COLUMN "api_keys"."status" IS 'ACTIVE: active\nINACTIVE: inactive\nREVOKED: revoked';
CREATE TABLE IF NOT EXISTS "subscriptions" (
    "id" UUID NOT NULL  PRIMARY KEY,
    "user_id" UUID NOT NULL UNIQUE,
    "provider" VARCHAR(32),
    "stripe_customer_id" VARCHAR(128),
    "stripe_subscription_id" VARCHAR(128),
    "stripe_price_id" VARCHAR(128),
    "provider_customer_id" VARCHAR(128),
    "provider_subscription_id" VARCHAR(128),
    "plan" VARCHAR(32) NOT NULL  DEFAULT 'trial',
    "status" VARCHAR(32) NOT NULL  DEFAULT 'trialing',
    "trial_ends_at" TIMESTAMPTZ,
    "current_period_start" TIMESTAMPTZ,
    "current_period_end" TIMESTAMPTZ,
    "cancel_at_period_end" BOOL NOT NULL  DEFAULT False,
    "canceled_at" TIMESTAMPTZ,
    "metadata" JSONB,
    "created_at" TIMESTAMPTZ NOT NULL  DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ NOT NULL  DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS "idx_subscriptio_user_id_16045d" ON "subscriptions" ("user_id");
CREATE INDEX IF NOT EXISTS "idx_subscriptio_stripe__1186f0" ON "subscriptions" ("stripe_customer_id");
CREATE INDEX IF NOT EXISTS "idx_subscriptio_stripe__003531" ON "subscriptions" ("stripe_subscription_id");
CREATE INDEX IF NOT EXISTS "idx_subscriptio_provide_af3272" ON "subscriptions" ("provider_customer_id");
CREATE INDEX IF NOT EXISTS "idx_subscriptio_provide_f15c7c" ON "subscriptions" ("provider_subscription_id");
COMMENT ON COLUMN "subscriptions"."plan" IS 'TRIAL: trial\nPRO: pro\nENTERPRISE: enterprise';
COMMENT ON COLUMN "subscriptions"."status" IS 'TRIALING: trialing\nACTIVE: active\nPAST_DUE: past_due\nCANCELED: canceled\nEXPIRED: expired\nINCOMPLETE: incomplete';
CREATE TABLE IF NOT EXISTS "invoices" (
    "id" UUID NOT NULL  PRIMARY KEY,
    "user_id" UUID NOT NULL,
    "provider" VARCHAR(32) NOT NULL,
    "provider_invoice_id" VARCHAR(128) NOT NULL,
    "amount" DECIMAL(12,2) NOT NULL,
    "currency" VARCHAR(8) NOT NULL,
    "status" VARCHAR(16) NOT NULL  DEFAULT 'pending',
    "description" VARCHAR(512),
    "hosted_url" VARCHAR(512),
    "pdf_url" VARCHAR(512),
    "paid_at" TIMESTAMPTZ,
    "metadata" JSONB,
    "created_at" TIMESTAMPTZ NOT NULL  DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ NOT NULL  DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT "uid_invoices_provide_e4cfca" UNIQUE ("provider", "provider_invoice_id")
);
CREATE INDEX IF NOT EXISTS "idx_invoices_user_id_fbd998" ON "invoices" ("user_id");
COMMENT ON COLUMN "invoices"."status" IS 'PENDING: pending\nPAID: paid\nFAILED: failed\nREFUNDED: refunded';
CREATE TABLE IF NOT EXISTS "stripe_events" (
    "event_id" VARCHAR(128) NOT NULL  PRIMARY KEY,
    "event_type" VARCHAR(64) NOT NULL,
    "received_at" TIMESTAMPTZ NOT NULL  DEFAULT CURRENT_TIMESTAMP
);
COMMENT ON TABLE "stripe_events" IS 'Idempotency log for Stripe webhooks.';
CREATE TABLE IF NOT EXISTS "passport_photos" (
    "id" UUID NOT NULL  PRIMARY KEY,
    "user_id" UUID NOT NULL,
    "photo_url" VARCHAR(1024) NOT NULL,
    "name" VARCHAR(255),
    "is_default" BOOL NOT NULL  DEFAULT False,
    "width_px" INT,
    "height_px" INT,
    "metadata" JSONB,
    "created_at" TIMESTAMPTZ NOT NULL  DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ NOT NULL  DEFAULT CURRENT_TIMESTAMP,
    "deleted_at" TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS "idx_passport_ph_user_id_c57615" ON "passport_photos" ("user_id");
CREATE TABLE IF NOT EXISTS "leads" (
    "id" UUID NOT NULL  PRIMARY KEY,
    "kind" VARCHAR(16) NOT NULL,
    "status" VARCHAR(16) NOT NULL  DEFAULT 'new',
    "name" VARCHAR(200) NOT NULL,
    "email" VARCHAR(254) NOT NULL,
    "topic" VARCHAR(120),
    "message" TEXT NOT NULL,
    "extra" JSONB,
    "ip_address" VARCHAR(64),
    "user_agent" VARCHAR(512),
    "reviewed_at" TIMESTAMPTZ,
    "reviewed_by_id" UUID,
    "created_at" TIMESTAMPTZ NOT NULL  DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ NOT NULL  DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS "idx_leads_kind_987f37" ON "leads" ("kind");
CREATE INDEX IF NOT EXISTS "idx_leads_status_44164f" ON "leads" ("status");
COMMENT ON COLUMN "leads"."kind" IS 'CONTACT: contact\nCAREERS: careers';
COMMENT ON COLUMN "leads"."status" IS 'NEW: new\nREVIEWED: reviewed\nARCHIVED: archived';
CREATE TABLE IF NOT EXISTS "aerich" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "version" VARCHAR(255) NOT NULL,
    "app" VARCHAR(100) NOT NULL,
    "content" JSONB NOT NULL
);"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        """


MODELS_STATE = (
    "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"
    "Qx91m/j5/2ElxQU="
)
