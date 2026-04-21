"""Auto-generated from https://jira.mongodb.org/browse/SERVER-109161

Title: [v8.0] Inconsistent state between opCtx and recovery unit after aborting WUOW
"""
from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoMongodb109161(GenericCustomBuildProblem):
    db_name   = "mongodb"
    issue_url = "https://jira.mongodb.org/browse/SERVER-109161"
    root_cause_description = (
        "[v8.0] Inconsistent state between opCtx and recovery unit after aborting WUOW. When user writes are disabled, aborting a wuow [does not update the state of the opCtx|https://github.com/mongodb/mongo/blob/6a7c140ee43487f6b596c4825ab3c29451ab9cad/src/mongo/db/storage/write_unit_of_work.cpp#L89-L91], leaving its internal _ruState inconsistent with the state of the recovery unit. It also doesn't take into account nesting of wuow's. All of this can lead to incorrect construction of subsequent wuow's, as it happened in [this patch|https://parsley.mongodb.com/test/mongodb_mongo_v8.0_s8_enterprise_rhel_8_64_bit_ese_patch_fdee6136ef374955be66f916ee70bb2fef18a07f_6899b0ec5bfaee00077da601_25_08_11_08_59_29/0/70127fa87f253700fdc47bafcdcfbe56?bookmarks=0,5068&shareLine=0]. I haven't checked whether this bug is in master, but it has been fixed after SERVER-93994."
    )
