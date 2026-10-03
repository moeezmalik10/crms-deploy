# =====================================================================
#  Your three cloud addresses. Fill each one in when the guide says so.
#  (No passwords here - this file is safe to keep in GitHub.)
# =====================================================================
$DEPLOY = @{
    BackendUrl  = ""   # step 3, e.g. https://crms-backend.onrender.com
    IdsUrl      = ""   # step 3, e.g. https://crms-ids.onrender.com
    FrontendUrl = ""   # step 4, e.g. https://crms-deploy.vercel.app
}

# Test login used by the checks (the starter admin from database\supabase_setup.sql)
$TEST_ADMIN = @{ Email = "admin@uog.edu.pk"; Password = "Admin@12345" }
