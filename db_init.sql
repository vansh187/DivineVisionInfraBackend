-- PostgreSQL initialization script for Divine users
-- Creates customer and broker user tables with ID format checks

CREATE TABLE IF NOT EXISTS divine_customer_users (
  id varchar(6) PRIMARY KEY CHECK (id ~ '^C[0-9]{5}$'),
  username varchar(255) UNIQUE NOT NULL,
  first_name varchar(150),
  last_name varchar(150),
  email varchar(255),
  phone varchar(50),
  password_hash varchar(255) NOT NULL,
  created_by varchar(255),
  created_date timestamptz DEFAULT now(),
  last_updated_by varchar(255),
  last_updated_date timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS divine_broker_users (
  id varchar(6) PRIMARY KEY CHECK (id ~ '^B[0-9]{5}$'),
  username varchar(255) UNIQUE NOT NULL,
  first_name varchar(150),
  last_name varchar(150),
  email varchar(255),
  phone varchar(50),
  password_hash varchar(255) NOT NULL,
  created_by varchar(255),
  created_date timestamptz DEFAULT now(),
  last_updated_by varchar(255),
  last_updated_date timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS divine_documents (
  id varchar(36) PRIMARY KEY,
  owner_id varchar(6) NOT NULL,
  owner_role varchar(10) NOT NULL CHECK (owner_role IN ('customer', 'broker')),
  document_type varchar(100) NOT NULL,
  form_data jsonb NOT NULL,
  storage_path text NOT NULL,
  status varchar(20) NOT NULL DEFAULT 'generated',
  created_date timestamptz DEFAULT now(),
  last_updated_date timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_divine_documents_owner_id ON divine_documents (owner_id);

CREATE TABLE IF NOT EXISTS divine_kyc_verifications (
  id varchar(36) PRIMARY KEY,
  owner_id varchar(6) NOT NULL,
  owner_role varchar(10) NOT NULL CHECK (owner_role IN ('customer', 'broker')),
  method varchar(20) NOT NULL CHECK (method IN ('qr', 'offline_xml')),
  verified boolean NOT NULL,
  masked_aadhaar varchar(20) NOT NULL,
  extracted_data jsonb NOT NULL,
  failure_reason varchar(100),
  created_date timestamptz DEFAULT now()
);
-- Note: by design, no column here ever holds a full/plaintext Aadhaar number - both the
-- Secure QR and Offline e-KYC XML formats only ever expose the last 4 digits (see
-- masked_aadhaar), never the full number, so there is nothing further to redact.

CREATE INDEX IF NOT EXISTS idx_divine_kyc_verifications_owner_id ON divine_kyc_verifications (owner_id);

CREATE TABLE IF NOT EXISTS divine_payments (
  id varchar(36) PRIMARY KEY,
  owner_id varchar(6) NOT NULL,
  owner_role varchar(10) NOT NULL CHECK (owner_role IN ('customer', 'broker')),
  amount numeric(12,2) NOT NULL,
  currency varchar(3) NOT NULL DEFAULT 'INR',
  status varchar(20) NOT NULL DEFAULT 'created' CHECK (status IN ('created', 'paid', 'failed')),
  method varchar(20) NOT NULL DEFAULT 'razorpay' CHECK (method IN ('razorpay', 'cash')),
  razorpay_order_id varchar(64),
  razorpay_payment_id varchar(64),
  razorpay_signature varchar(255),
  notes jsonb,
  created_date timestamptz DEFAULT now(),
  last_updated_date timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_divine_payments_owner_id ON divine_payments (owner_id);
CREATE INDEX IF NOT EXISTS idx_divine_payments_razorpay_order_id ON divine_payments (razorpay_order_id);
