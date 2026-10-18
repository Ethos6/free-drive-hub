import pytest

from drivehub.backends.base import BackendError
from drivehub.hub import HubError

from .conftest import MB, blob


async def names(hub, path="/"):
    return [e.name for e in await hub.ls(path)]


async def test_same_folder_in_two_accounts_is_merged(hub, backends):
    a, b = backends
    da = a.add_folder(a._root, "Photos")
    db = b.add_folder(b._root, "Photos")
    a.add_file(da, "one.jpg", b"1")
    b.add_file(db, "two.jpg", b"2")
    root = await hub.ls("/")
    assert [e.name for e in root] == ["Photos"]
    assert {acc.label for acc, _ in root[0].sources} == {"alpha", "beta"}
    assert await names(hub, "/Photos") == ["one.jpg", "two.jpg"]


async def test_clashing_file_names_get_account_label(hub, backends):
    a, b = backends
    a.add_file(a._root, "notes.txt", b"a")
    b.add_file(b._root, "notes.txt", b"b")
    b.add_file(b._root, "README", b"r")
    a.add_folder(a._root, "README")
    assert await names(hub) == ["README", "notes (beta).txt", "notes.txt", "README (beta)"]
    e = await hub.stat("/notes (beta).txt")
    assert e.sources[0][0].label == "beta"


async def test_folders_listed_before_files(hub, backends):
    a, _ = backends
    a.add_file(a._root, "a.txt", b"")
    a.add_folder(a._root, "z-folder")
    assert await names(hub) == ["z-folder", "a.txt"]


async def test_workspace_docs_get_export_extension(hub, backends):
    a, _ = backends
    a.add_file(a._root, "Plan", b"doc", "application/vnd.google-apps.document")
    e = await hub.stat("/Plan.docx")
    assert e.mime.endswith("wordprocessingml.document")
    _, dl = await hub.read("/Plan.docx")
    assert dl.mime.endswith("wordprocessingml.document")


async def test_slash_in_drive_name_is_replaced(hub, backends):
    a, _ = backends
    a.add_file(a._root, "a/b.txt", b"x")
    assert await names(hub) == ["a∕b.txt"]
    await hub.stat("/a∕b.txt")


async def test_upload_goes_to_account_with_most_free_space(hub, backends):
    a, b = backends
    a.add_folder(a._root, "Docs")  # folder exists only in the smaller account
    await hub.write("/Docs/report.pdf", blob(MB), MB)
    e = await hub.stat("/Docs/report.pdf")
    assert e.sources[0][0].label == "beta"  # beta has 20 MB free, alpha 10 MB
    assert {acc.label for acc, _ in (await hub.stat("/Docs")).sources} == {"alpha", "beta"}


async def test_upload_spills_to_next_account_when_full(hub, backends):
    a, b = backends
    await hub.write("/big1", blob(15 * MB), 15 * MB)   # beta: 5 MB left, alpha: 10 MB
    await hub.write("/big2", blob(8 * MB), 8 * MB)     # only alpha fits
    assert (await hub.stat("/big1")).sources[0][0].label == "beta"
    assert (await hub.stat("/big2")).sources[0][0].label == "alpha"
    with pytest.raises(HubError) as ei:
        await hub.write("/big3", blob(6 * MB), 6 * MB)
    assert ei.value.status == 507


async def test_overwrite_updates_file_in_place(hub, backends):
    a, _ = backends
    fid = a.add_file(a._root, "x.txt", b"old")
    await hub.write("/x.txt", blob(10), 10)
    assert a.data[fid] == b"x" * 10
    assert await names(hub) == ["x.txt"]


async def test_mkdir_and_errors(hub):
    await hub.mkdir("/New")
    assert await names(hub) == ["New"]
    with pytest.raises(HubError) as ei:
        await hub.mkdir("/New")
    assert ei.value.status == 405
    with pytest.raises(HubError) as ei:
        await hub.mkdir("/missing/child")
    assert ei.value.status == 404
    with pytest.raises(HubError):
        await hub.ls("/../etc")


async def test_rename_and_move(hub, backends):
    a, b = backends
    a.add_file(a._root, "a.txt", b"a")
    b.add_folder(b._root, "Target")  # destination folder only in the other account
    await hub.move("/a.txt", "/renamed.txt")
    assert await names(hub) == ["Target", "renamed.txt"]
    await hub.move("/renamed.txt", "/Target/moved.txt")
    e = await hub.stat("/Target/moved.txt")
    assert e.sources[0][0].label == "alpha"  # stayed in its own account
    assert await names(hub, "/Target") == ["moved.txt"]


async def test_move_merged_folder_moves_every_part(hub, backends):
    a, b = backends
    a.add_file(a.add_folder(a._root, "Music"), "a.mp3", b"a")
    b.add_file(b.add_folder(b._root, "Music"), "b.mp3", b"b")
    await hub.mkdir("/Archive")
    await hub.move("/Music", "/Archive/Music")
    assert await names(hub) == ["Archive"]
    assert await names(hub, "/Archive/Music") == ["a.mp3", "b.mp3"]


async def test_move_into_itself_rejected(hub):
    await hub.mkdir("/A")
    with pytest.raises(HubError) as ei:
        await hub.move("/A", "/A/B")
    assert ei.value.status == 409


async def test_move_existing_destination(hub, backends):
    a, _ = backends
    a.add_file(a._root, "1.txt", b"1")
    a.add_file(a._root, "2.txt", b"2")
    with pytest.raises(HubError) as ei:
        await hub.move("/1.txt", "/2.txt")
    assert ei.value.status == 412
    await hub.move("/1.txt", "/2.txt", overwrite=True)
    assert await names(hub) == ["2.txt"]


async def test_copy_file_and_folder(hub, backends):
    a, _ = backends
    d = a.add_folder(a._root, "Src")
    a.add_file(a.add_folder(d, "Sub"), "deep.txt", b"deep")
    a.add_file(d, "top.txt", b"top")
    await hub.copy("/Src/top.txt", "/top-copy.txt")
    await hub.copy("/Src", "/Dst")
    assert await names(hub, "/Dst") == ["Sub", "top.txt"]
    assert await names(hub, "/Dst/Sub") == ["deep.txt"]
    assert "top-copy.txt" in await names(hub)


async def test_copy_falls_back_to_other_account_when_full(hub, backends):
    a, b = backends
    a.add_file(a._root, "big.bin", b"x" * (6 * MB))
    await hub.copy("/big.bin", "/big2.bin")  # alpha has 4 MB left, so it lands in beta
    assert (await hub.stat("/big2.bin")).sources[0][0].label == "beta"


async def test_transfer_file_between_accounts(hub, backends):
    a, b = backends
    a.add_file(a.add_folder(a._root, "Docs"), "cv.pdf", b"pdf" * 100)
    await hub.transfer("/Docs/cv.pdf", 2)
    e = await hub.stat("/Docs/cv.pdf")
    assert e.sources[0][0].label == "beta"
    assert (await a.quota()).used == 0
    assert (await b.quota()).used == 300


async def test_transfer_folder(hub, backends):
    a, b = backends
    d = a.add_folder(a._root, "Album")
    a.add_file(d, "1.png", b"1")
    a.add_file(a.add_folder(d, "Raw"), "2.raw", b"22")
    await hub.transfer("/Album", 2)
    album = await hub.stat("/Album")
    assert [acc.label for acc, _ in album.sources] == ["beta"]
    assert await names(hub, "/Album/Raw") == ["2.raw"]
    assert (await a.quota()).used == 0


async def test_transfer_needs_space(hub, backends):
    a, b = backends
    b.add_file(b._root, "huge", b"x" * (12 * MB))
    with pytest.raises(HubError) as ei:
        await hub.transfer("/huge", 1)
    assert ei.value.status == 507


async def test_delete_merged_folder_trashes_all_parts(hub, backends):
    a, b = backends
    fa = a.add_folder(a._root, "Old")
    fb = b.add_folder(b._root, "Old")
    await hub.delete("/Old")
    assert fa in a.trashed and fb in b.trashed
    assert await names(hub) == []
    with pytest.raises(HubError):
        await hub.delete("/")


async def test_search_reports_folder_path(hub, backends):
    a, b = backends
    a.add_file(a.add_folder(a.add_folder(a._root, "Work"), "2026"), "invoice-april.pdf", b"i")
    b.add_file(b._root, "Invoice-may.pdf", b"i")
    res = await hub.search("invoice")
    assert [(r["name"], r["folder"], r["account"]) for r in res] == [
        ("invoice-april.pdf", "/Work/2026", "alpha"), ("Invoice-may.pdf", "/", "beta")]


async def test_broken_account_does_not_break_listing(hub, backends):
    a, b = backends
    a.add_file(a._root, "ok.txt", b"ok")

    async def boom(*args, **kw):
        raise BackendError("token revoked", 401)
    b.list_folders = boom
    b.root_id = boom
    b.quota = boom
    assert await names(hub) == ["ok.txt"]
    assert "token revoked" in hub.errors[2]
    await hub.write("/new.txt", blob(5), 5)  # still writes to the working account
    assert (await hub.stat("/new.txt")).sources[0][0].label == "alpha"


async def test_share_returns_link_per_account(hub, backends):
    a, b = backends
    a.add_folder(a._root, "Shared")
    b.add_folder(b._root, "Shared")
    links = await hub.share("/Shared")
    assert [l["account"] for l in links] == ["alpha", "beta"]


async def test_index_is_cached(hub, backends):
    a, _ = backends
    a.add_folder(a._root, "X")
    await hub.ls("/X")
    await hub.ls("/X")
    assert a.calls.count("list_folders") == 1
