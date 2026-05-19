import { describe, it, expect } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {
  DataTable,
  type ColumnDef,
} from "@/components/ui/data-table";
import { Button } from "@/components/ui/button";
import { t } from "@/lib/i18n";

// Strings are derived from the i18n dictionary, not hardcoded English: the
// test asserts the table BEHAVIOUR (row count, filtering, sorting,
// pagination) via the same keys the component renders, so a translation
// change cannot silently break it.
const rowCount = (count: number) =>
  t("common.dataTable.rowCount", { count });
const pageOf = (page: number, total: number) =>
  t("common.dataTable.pageOf", { page, total });
const SEARCH_PLACEHOLDER = t("common.dataTable.search");
const ALL = t("common.dataTable.all");
const EMPTY = t("common.dataTable.empty");

interface Row {
  name: string;
  score: number;
  status: string;
}

const data: Row[] = [
  { name: "alice", score: 30, status: "new" },
  { name: "bob", score: 90, status: "following" },
  { name: "carol", score: 60, status: "new" },
  { name: "dave", score: 10, status: "rejected" },
];

const columns: ColumnDef<Row, unknown>[] = [
  { accessorKey: "name", header: "Name" },
  {
    accessorKey: "score",
    header: ({ column }) => (
      <Button
        onClick={() => column.toggleSorting(column.getIsSorted() === "asc")}
      >
        Score
      </Button>
    ),
  },
  {
    accessorKey: "status",
    header: "Status",
    filterFn: (row, id, value: string) =>
      !value || row.getValue<string>(id) === value,
  },
];

function bodyRows() {
  // first <tr> is the header row
  const rows = screen.getAllByRole("row");
  return rows.slice(1);
}

describe("ui/DataTable", () => {
  it("renders all rows and a row count", () => {
    render(<DataTable columns={columns} data={data} />);
    expect(bodyRows()).toHaveLength(4);
    expect(screen.getByText(rowCount(4))).toBeInTheDocument();
  });

  it("renders the default honest empty state when there is no data", () => {
    render(<DataTable columns={columns} data={[]} />);
    expect(screen.getByText(EMPTY)).toBeInTheDocument();
    expect(screen.getByText(rowCount(0))).toBeInTheDocument();
  });

  it("renders a caller-supplied emptyState node when provided", () => {
    render(
      <DataTable
        columns={columns}
        data={[]}
        emptyState="No candidates yet. Run discovery."
      />
    );
    expect(
      screen.getByText("No candidates yet. Run discovery.")
    ).toBeInTheDocument();
  });

  it("sorts ascending then descending by the score column header", async () => {
    render(<DataTable columns={columns} data={data} />);

    const sortBtn = screen.getByRole("button", { name: "Score" });
    await userEvent.click(sortBtn); // asc
    let cells = bodyRows().map(
      (r) => within(r).getAllByRole("cell")[1].textContent
    );
    expect(cells).toEqual(["10", "30", "60", "90"]);

    await userEvent.click(sortBtn); // desc
    cells = bodyRows().map(
      (r) => within(r).getAllByRole("cell")[1].textContent
    );
    expect(cells).toEqual(["90", "60", "30", "10"]);
  });

  it("free-text search filters by the configured searchKey column", async () => {
    render(
      <DataTable columns={columns} data={data} searchKey="name" />
    );
    await userEvent.type(
      screen.getByPlaceholderText(SEARCH_PLACEHOLDER),
      "car"
    );
    expect(bodyRows()).toHaveLength(1);
    expect(screen.getByText("carol")).toBeInTheDocument();
    expect(screen.getByText(rowCount(1))).toBeInTheDocument();
  });

  it("faceted filter narrows rows by exact value and 'All' resets", async () => {
    render(
      <DataTable
        columns={columns}
        data={data}
        facetedFilters={[
          {
            columnId: "status",
            title: "Status",
            options: [
              { label: "New", value: "new" },
              { label: "Following", value: "following" },
            ],
          },
        ]}
      />
    );

    await userEvent.click(screen.getByRole("button", { name: /Status/ }));
    await userEvent.click(
      await screen.findByRole("menuitemcheckbox", { name: "New" })
    );
    expect(bodyRows()).toHaveLength(2); // alice + carol

    await userEvent.click(screen.getByRole("button", { name: /Status/ }));
    await userEvent.click(
      await screen.findByRole("menuitemcheckbox", { name: ALL })
    );
    expect(bodyRows()).toHaveLength(4);
  });

  it("paginates when rows exceed pageSize", async () => {
    const many: Row[] = Array.from({ length: 7 }, (_, i) => ({
      name: `u${i}`,
      score: i,
      status: "new",
    }));
    render(<DataTable columns={columns} data={many} pageSize={3} />);

    expect(bodyRows()).toHaveLength(3);
    expect(screen.getByText(pageOf(1, 3))).toBeInTheDocument();

    // The two pagination buttons are the only icon buttons; next = last
    const navButtons = screen
      .getAllByRole("button")
      .filter((b) => b.classList.contains("h-7"));
    const nextBtn = navButtons[navButtons.length - 1];
    await userEvent.click(nextBtn);
    expect(screen.getByText(pageOf(2, 3))).toBeInTheDocument();

    // total filtered count still reflects all rows
    expect(screen.getByText(rowCount(7))).toBeInTheDocument();
  });
});
