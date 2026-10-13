def average(values):
    return sum(values) / len(values) if values else 0

def grade(mark):
    return 'A' if mark >= 80 else 'B' if mark >= 60 else 'C'
