from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE "document_comments"
            ADD COLUMN IF NOT EXISTS "field_key" VARCHAR(120);
        COMMENT ON COLUMN "document_participants"."status" IS
            'INVITED: invited\nVIEWED: viewed\nSIGNED: signed\nAPPROVED: approved\nDECLINED: declined\nREJECTED: rejected\nREVOKED: revoked';
    """


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE "document_comments" DROP COLUMN IF EXISTS "field_key";
    """
