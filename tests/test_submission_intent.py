from dradar.submission_intent import submission_payload_sha256


def test_upload_intent_digest_contract_vector(tmp_path):
    patch = tmp_path / "model.patch"
    patch.write_bytes(b"diff --git a/x b/x\n")
    assert submission_payload_sha256(
        assignment_id="a1",
        session_id="session-1234",
        resume_generation=2,
        outcome="completed",
        meta={"z": 1, "a": "值"},
        patch=patch,
        trajectory=None,
        result=None,
        trajectory_bundle=None,
    ) == "cac8117a75aba37bdae5ab37199842e1bb5896c5850879b8525fabb5ea8145b8"
