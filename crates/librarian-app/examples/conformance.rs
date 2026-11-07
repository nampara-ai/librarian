use std::io::{self, BufRead};

fn main() -> io::Result<()> {
    for line in io::stdin().lock().lines() {
        println!("{}", librarian_app::execute_json(&line?));
    }
    Ok(())
}
