-- Runs once, the first time the Postgres volume is created.

-- pgvector for embeddings in the main database.
CREATE EXTENSION IF NOT EXISTS vector;

-- One schema per concern, all in one database.
CREATE SCHEMA IF NOT EXISTS kb;          -- knowledge base chunks and embeddings
CREATE SCHEMA IF NOT EXISTS app;         -- approvals, audit log, webhook events
CREATE SCHEMA IF NOT EXISTS jobs;        -- the job queue
CREATE SCHEMA IF NOT EXISTS checkpoints; -- LangGraph run state

-- Phoenix keeps its own tables in a separate database.
CREATE DATABASE phoenix;
