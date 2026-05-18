import { describe, it, expect } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {
  DataTable,
  type ColumnDef,
} from "@/components/ui/data-table";
import { Button } from "@/components/ui/button";

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
    expect(screen.getByText("4 rows")).toBeInTheDocument();
  });

  it("renders the default honest empty state when there is no data", () => {
    render(<DataTable columns={columns} data={[]} />);
    expect(
      screen.getByText(/No rows to show yet\./)
    ).toBeInTheDocument();
    expect(screen.getByText("0 rows")).toBeInTheDocument();
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
    await userEvent.type(screen.getByPlaceholderText("Search…"), "car");
    expect(bodyRows()).toHaveLength(1);
    expect(screen.getByText("carol")).toBeInTheDocument();
    expect(screen.getByText("1 row")).toBeInTheDocument();
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
      await screen.findByRole("menuitemcheckbox", { name: "All" })
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
    expect(screen.getByText("Page 1 of 3")).toBeInTheDocument();

    // The two pagination buttons are the only icon buttons; next = last
    const navButtons = screen
      .getAllByRole("button")
      .filter((b) => b.classList.contains("h-7"));
    const nextBtn = navButtons[navButtons.length - 1];
    await userEvent.click(nextBtn);
    expect(screen.getByText("Page 2 of 3")).toBeInTheDocument();

    // total filtered count still reflects all rows
    expect(screen.getByText("7 rows")).toBeInTheDocument();
  });
});
