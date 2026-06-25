from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS "commission_events" (
    "id" UUID NOT NULL PRIMARY KEY,
    "referral_id" UUID NOT NULL,
    "invoice_id" UUID,
    "amount" DECIMAL(14,2) NOT NULL,
    "currency" VARCHAR(3) NOT NULL,
    "rate" DECIMAL(5,4) NOT NULL,
    "occurred_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS "idx_commission__referra_054c0b" ON "commission_events" ("referral_id");
COMMENT ON TABLE "commission_events" IS 'One row per accrued commission — paid invoice on the referred user.';
        CREATE TABLE IF NOT EXISTS "referrals" (
    "id" UUID NOT NULL PRIMARY KEY,
    "referrer_user_id" UUID NOT NULL,
    "referred_user_id" UUID NOT NULL UNIQUE,
    "code_used" VARCHAR(24) NOT NULL,
    "status" VARCHAR(16) NOT NULL DEFAULT 'signed_up',
    "signed_up_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "commission_started_at" TIMESTAMPTZ,
    "commission_expires_at" TIMESTAMPTZ,
    "total_commission" DECIMAL(14,2) NOT NULL DEFAULT 0,
    "commission_currency" VARCHAR(3) NOT NULL DEFAULT 'USD',
    "paid_out_at" TIMESTAMPTZ,
    "paid_out_amount" DECIMAL(14,2),
    "payout_reference" VARCHAR(128),
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS "idx_referrals_referre_443c0d" ON "referrals" ("referrer_user_id");
CREATE INDEX IF NOT EXISTS "idx_referrals_code_us_6602c3" ON "referrals" ("code_used");
CREATE INDEX IF NOT EXISTS "idx_referrals_status_2bd0a7" ON "referrals" ("status");
COMMENT ON COLUMN "referrals"."status" IS 'SIGNED_UP: signed_up\nCONVERTED: converted\nEXPIRED: expired\nVOID: void';
        ALTER TABLE "users" ADD "referral_code" VARCHAR(24)  UNIQUE;"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        DROP INDEX IF EXISTS "uid_users_referra_80f3c6";
        ALTER TABLE "users" DROP COLUMN "referral_code";
        DROP TABLE IF EXISTS "referrals";
        DROP TABLE IF EXISTS "commission_events";"""


MODELS_STATE = (
    "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"
    "CTZpn39xZjctBt4uf/A+P28gM="
)
